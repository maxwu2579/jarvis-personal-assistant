"""OpenAIEmbeddingProvider：openai-compatible 语义 Embedding（Phase 5B 显式
门控 + Phase 5C 生产化：base URL / 有限重试 / 响应严格验证）。

诚实边界（README 同步声明）：
- 这是唯一可以自称「真实神经语义 embedding」的 provider 路径；
  只有它实际运行过的结果才能被报告为语义 embedding（LocalHash 永远不是）；
- **绝不静默回退**：外部 provider 失败 → 异常向上传播，索引事务整体回滚
  （不留下半套向量，不伪装成功）；
- 普通 pytest 完全 mock（client_factory 注入替身，零网络）；
- 真实联网测试必须 RUN_LIVE_EMBEDDING_TESTS=1 显式启用，且仍需真实 Key
  与真实额度；绝不自动消费 API 额度；
- Key 只经 pydantic-settings 从环境 / .env 读取（embedding_api_key），
  本模块不读文件、不打日志（不打印输入全文、Key、供应商原始响应）；
- 超时、批量上限、稳定脱敏错误映射：任何上游异常 → EmbeddingError
  稳定码，不含 URL/Key/原始响应体；
- 可选 base_url（EMBEDDING_BASE_URL）：OpenAI 兼容端点。**不假设任何
  非 OpenAI 供应商（如 DeepSeek）一定提供 embedding endpoint**——运行期
  以实际响应验证为准（数量/顺序/索引/维度/有限数值）。

重试策略（Phase 5C）：
- 仅明确可重试错误重试：429 / 部分 5xx / timeout；指数退避
  backoff * 2**attempt，最大次数 = embedding_max_retries（有限）；
- 401/403（EMBEDDING_INVALID_KEY）、响应非法（EMBEDDING_RESPONSE_INVALID /
  INTERNAL）、维度错误 → 绝不重试；
- 重试等待通过可注入 retry_sleeper 实现（测试注入记录式替身，
  单元测试中不发生真实 sleep）。

维度契约：
- 输出维度必须 == 配置维度（EMBEDDING_DIMENSION）；
- 构造时校验模型已知维度集合（text-embedding-3-small：512/1536/3072；
  text-embedding-3-large：256/1024/3072；text-embedding-ada-002：1536）；
  未知模型 → 拒绝启动（宁失败不猜测；兼容服务请使用已知 OpenAI 模型名）；
- 维度不匹配（配置 vs 模型）→ EMBEDDING_DIMENSION_MISMATCH，绝不截断/填充。
"""

import logging
import time
from typing import Any, Callable

from app.core.config import settings
from app.embeddings.base import (
    EmbeddingError,
    EmbeddingProvider,
    EmbeddingResponseError,
)

logger = logging.getLogger(__name__)

# 模型 → 允许的维度（OpenAI 官方；text-embedding-3 系列支持 dimensions 参数）
_KNOWN_MODEL_DIMENSIONS: dict[str, frozenset[int]] = {
    "text-embedding-3-small": frozenset({512, 1536, 3072}),
    "text-embedding-3-large": frozenset({256, 1024, 3072}),
    "text-embedding-ada-002": frozenset({1536}),
}

# 仅这些稳定码可重试（429/5xx/timeout）；其余（401/403、非法响应）绝不重试
_RETRYABLE_CODES = frozenset(
    {"EMBEDDING_TIMEOUT", "EMBEDDING_RATE_LIMITED", "EMBEDDING_UPSTREAM_ERROR"}
)


def _classify_error(exc: Exception) -> tuple[str, bool]:
    """上游异常 → (稳定错误码, 是否可重试)。

    基于异常类型名与 status_code（不依赖供应商响应体内容）；
    5xx 与 429/timeout 可重试，401/403/其他 4xx 不可重试。
    """
    name = type(exc).__name__.lower()
    status = getattr(exc, "status_code", None)
    if "timeout" in name:
        return "EMBEDDING_TIMEOUT", True
    if "ratelimit" in name or "rate" in name:
        return "EMBEDDING_RATE_LIMITED", True
    if "auth" in name or "permission" in name or "apikey" in name:
        return "EMBEDDING_INVALID_KEY", False
    # 状态码判定兜底（异常类名未命中时）：429 恒限流，5xx 可重试，
    # 其余 4xx 不可重试
    if isinstance(status, int):
        if status == 429:
            return "EMBEDDING_RATE_LIMITED", True
        if 500 <= status < 600:
            return "EMBEDDING_UPSTREAM_ERROR", True
    return "EMBEDDING_UPSTREAM_ERROR", False


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """OpenAI 兼容 Embeddings API 批量向量（显式配置 + 显式 Key 才可用）。"""

    def __init__(
        self,
        dimension: int | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        base_url: str | None = None,
        max_retries: int | None = None,
        retry_backoff_seconds: float | None = None,
        retry_sleeper: Callable[[float], None] | None = None,
        client_factory: Any | None = None,
    ):
        self._dimension = dimension or settings.embedding_dimension
        self._model = (model or settings.embedding_model or "").strip()
        self._api_key = (
            api_key if api_key is not None else settings.embedding_api_key
        )
        self._timeout = timeout_seconds or settings.embedding_timeout_seconds
        self._base_url = (
            base_url if base_url is not None else settings.embedding_base_url
        ).strip() or None
        self._max_retries = (
            max_retries
            if max_retries is not None
            else settings.embedding_max_retries
        )
        self._retry_backoff = (
            retry_backoff_seconds
            if retry_backoff_seconds is not None
            else settings.embedding_retry_backoff_seconds
        )
        # 可注入 sleeper：测试注入记录式替身，单元测试绝不真实 sleep
        self._retry_sleeper = retry_sleeper or time.sleep

        if not self._model:
            raise EmbeddingError(
                "EMBEDDING_NOT_CONFIGURED",
                "embedding_provider=openai requires EMBEDDING_MODEL (config validator)",
            )
        if not self._api_key:
            raise EmbeddingError(
                "EMBEDDING_NOT_CONFIGURED",
                "openai embedding provider requires EMBEDDING_API_KEY to be set",
            )
        allowed = _KNOWN_MODEL_DIMENSIONS.get(self._model)
        if allowed is None:
            raise EmbeddingError(
                "EMBEDDING_UNKNOWN_MODEL",
                f"unknown embedding model {self._model!r}; "
                f"known models: {sorted(_KNOWN_MODEL_DIMENSIONS)}",
            )
        if self._dimension not in allowed:
            raise EmbeddingError(
                "EMBEDDING_DIMENSION_MISMATCH",
                f"embedding dimension {self._dimension} is not valid for model "
                f"{self._model!r} (allowed: {sorted(allowed)})",
            )

        # 客户端延迟创建；client_factory 仅供测试注入替身（普通测试零网络）
        self._client_factory = client_factory
        self._client = None

    @property
    def provider_name(self) -> str:
        return "openai-compatible"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model

    def capabilities(self) -> dict:
        return {
            "semantic": True,
            "network": True,
            "deterministic": False,
            "description": (
                "openai-compatible: real neural semantic embedding via an "
                "OpenAI-compatible endpoint (explicitly configured and gated)"
            ),
        }

    def _get_client(self):
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:
                import openai

                self._client = openai.OpenAI(
                    api_key=self._api_key,
                    base_url=self._base_url,
                    timeout=self._timeout,
                )
        return self._client

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        client = self._get_client()
        kwargs: dict[str, Any] = {"model": self._model, "input": texts}
        if self._model in ("text-embedding-3-small", "text-embedding-3-large"):
            kwargs["dimensions"] = self._dimension
        attempt = 0
        while True:
            try:
                response = client.embeddings.create(**kwargs)
                return self._validate_response(texts, response)
            except EmbeddingError:
                # 响应非法 / 维度错误：重试不会变好，立即失败
                raise
            except Exception as exc:  # noqa: BLE001 - 供应商异常统一收敛
                code, retryable = _classify_error(exc)
                if not retryable or attempt >= self._max_retries:
                    logger.warning(
                        "embedding.openai_failed code=%s model=%s texts=%s",
                        code,
                        self._model,
                        len(texts),
                    )
                    raise EmbeddingError(code, _MESSAGE_FOR[code]) from exc
                self._retry_sleeper(self._retry_backoff * (2**attempt))
                attempt += 1

    def _validate_response(
        self, texts: list[str], response: Any
    ) -> list[list[float]]:
        """验证响应数量、顺序（按 index）、维度与有限数值。

        任何异常 → 稳定错误（EMBEDDING_RESPONSE_INVALID），绝不重试：
        - 响应不可读 / 非列表 → 错误；
        - 数量不一致 → 错误；
        - 缺失 index / index 非整数 → 错误（顺序无法判定）；
        - index 乱序但完整 → 按 index 确定性重排；
        - 维度 / NaN / Infinity → 基类 _validate_vectors 拒绝。
        """
        try:
            data = list(response.data)
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingResponseError(
                "embedding response is not readable"
            ) from exc
        if len(data) != len(texts):
            raise EmbeddingResponseError(
                f"embedding response count mismatch (got {len(data)}, "
                f"expected {len(texts)})"
            )
        indexed: list[tuple[int, list[float]]] = []
        for item in data:
            index = getattr(item, "index", None)
            if not isinstance(index, int):
                raise EmbeddingResponseError(
                    "embedding response item has no valid index"
                )
            try:
                vector = list(item.embedding)
            except Exception as exc:  # noqa: BLE001
                raise EmbeddingResponseError(
                    "embedding response item has no embedding"
                ) from exc
            indexed.append((index, vector))
        indexed.sort(key=lambda pair: pair[0])
        return self._validate_vectors([vector for _, vector in indexed])


_MESSAGE_FOR: dict[str, str] = {
    "EMBEDDING_TIMEOUT": "embedding provider timed out",
    "EMBEDDING_RATE_LIMITED": "embedding provider rate limit exceeded",
    "EMBEDDING_INVALID_KEY": "embedding provider rejected the API key",
    "EMBEDDING_UPSTREAM_ERROR": "embedding provider returned an error",
    "EMBEDDING_RESPONSE_INVALID": "embedding provider returned an invalid response",
}
