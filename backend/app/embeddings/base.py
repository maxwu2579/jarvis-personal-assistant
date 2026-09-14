"""EmbeddingProvider 抽象（Phase 4B）。

契约：
- Provider 只负责「文本 → 向量」：不访问数据库、不执行检索、不调用 LLM；
- 输入顺序与输出顺序严格一致；空列表返回空列表；
- 输出维度固定（dimension）；所有数值必须是有限浮点数；
- 对相同输入必须产生完全可重复的向量（无随机性）；
- 空白 query / 超长文本 / 超大批量由调用方（schema/service）或本层拒绝，
  不生成无意义向量。

实现约定：子类只实现 _embed_batch（核心批量算法）；基类负责输入校验、
批量上限、数值有限性校验与错误包装（稳定错误类型，不泄露内部异常细节）。
"""

import math
from abc import ABC, abstractmethod

from app.core.config import settings


class EmbeddingError(Exception):
    """嵌入层稳定错误（code + sanitized message，不泄露内部异常细节）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class EmbeddingInputError(EmbeddingError):
    """输入非法：空白文本 / 超长文本 / 超大批量。"""

    def __init__(self, message: str):
        super().__init__("INVALID_EMBEDDING_INPUT", message)


class EmbeddingBatchTooLargeError(EmbeddingError):
    def __init__(self, batch_size: int, limit: int):
        super().__init__(
            "EMBEDDING_BATCH_TOO_LARGE",
            f"embedding batch too large ({batch_size} > {limit})",
        )


class EmbeddingInternalError(EmbeddingError):
    """算法内部失败（含非有限数值防御）。"""

    def __init__(self, message: str):
        super().__init__("EMBEDDING_INTERNAL_ERROR", message)


class EmbeddingResponseError(EmbeddingError):
    """外部 provider 响应非法（schema / 数量 / 顺序 / 索引缺失）。

    明确不可重试：响应内容错误重试不会变好，必须立即失败。
    """

    def __init__(self, message: str):
        super().__init__("EMBEDDING_RESPONSE_INVALID", message)


class EmbeddingProvider(ABC):
    """文本 → 向量 的抽象接口（Phase 4B 基础 + Phase 5C 生产化扩展）。

    Phase 5C 协议契约（provider 生产化）：
    - provider_name：稳定 provider 标识，持久化到向量行
      （stale 判定的身份维度之一；默认类名，子类覆盖为稳定名）；
    - max_batch_size：本 provider 单次批量上限（默认取配置，子类可收紧）；
    - capabilities()：能力/诊断描述（health 与 README 使用，无密钥）；
    - 维度契约：provider.dimension / 配置维度 / PG VECTOR(n) / 已存
      metadata 四者一致；任何不匹配 → 稳定错误，绝不截断/补零/转换。
    """

    @property
    @abstractmethod
    def dimension(self) -> int:
        """输出向量维度（固定）。"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """模型/算法版本标识，例如 local-hash-v1。持久化到 ChunkEmbedding。"""

    @property
    def provider_name(self) -> str:
        """稳定 provider 标识（持久化身份维度；默认类名，子类覆盖）。"""
        return type(self).__name__

    @property
    def max_batch_size(self) -> int:
        """本 provider 的单次批量上限（默认配置值；子类可声明更小上限）。"""
        return settings.embedding_batch_max

    def capabilities(self) -> dict:
        """能力/诊断描述（health/README 用，不含任何密钥或向量）。

        默认：非语义、零网络、确定性（local-hash/fake 同此；外部 provider
        子类必须覆盖 semantic=True）。
        """
        return {
            "semantic": False,
            "network": False,
            "deterministic": True,
            "description": f"{type(self).__name__} (non-semantic local vectorizer)",
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入：顺序与输入严格一致；空列表返回空列表。

        批量上限取 provider.max_batch_size（防内存滥用与外部批量限制），
        单文本长度上限来自配置。
        """
        if not texts:
            return []
        if len(texts) > self.max_batch_size:
            raise EmbeddingBatchTooLargeError(len(texts), self.max_batch_size)
        for text in texts:
            self._validate_text(text)
        vectors = self._embed_batch(list(texts))
        return self._validate_vectors(vectors)

    def embed_query(self, text: str) -> list[float]:
        """单个查询向量。空白文本必须由调用方拒绝（这里也兜底校验）。"""
        self._validate_text(text)
        return self._validate_vectors(self._embed_batch([text]))[0]

    # ---- 子类实现 ----

    @abstractmethod
    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """核心批量算法（子类实现，确定性、无随机）。"""

    # ---- 基类防御 ----

    def _validate_text(self, text: str) -> None:
        if text is None or not text.strip():
            raise EmbeddingInputError("text must not be blank")
        if len(text) > settings.embedding_max_text_chars:
            raise EmbeddingInputError(
                f"text too long ({len(text)} chars > "
                f"{settings.embedding_max_text_chars})"
            )

    def _validate_vectors(self, vectors: list[list[float]]) -> list[list[float]]:
        """维度固定 + 全部有限数值（拒绝 NaN/Infinity）。"""
        for vec in vectors:
            if len(vec) != self.dimension:
                raise EmbeddingInternalError(
                    f"embedding dimension mismatch (got {len(vec)}, "
                    f"expected {self.dimension})"
                )
            for value in vec:
                if not math.isfinite(value):
                    raise EmbeddingInternalError(
                        "embedding contains non-finite values"
                    )
        return vectors
