"""OpenAI 兼容 Provider：使用官方 openai SDK，调用链透明。

- 不重试（生成型调用不视为幂等）；
- 超时由 LLM_TIMEOUT_SECONDS 控制，映射为 LLMTimeoutError；
- 其他上游错误映射为 LLMUpstreamError，不向上抛出原始响应体；
- 不保存 / 记录 API Key；
- response_schema 非空时启用官方 Structured Output（response_format=json_schema，
  strict 模式），schema 经 _prepare_strict_schema 适配为 OpenAI 严格模式约束。

共享辅助（build_chat_client / map_sdk_error / to_llm_response）同时被
deepseek_provider 复用，避免两份难以维护的复制粘贴；子类只需覆盖
_response_format 与 generate 的消息前置逻辑。
"""

import logging
import time

from pydantic import BaseModel

from app.llm.base import LLMProvider, LLMResponse, LLMTimeoutError, LLMUpstreamError, ProviderMessage

logger = logging.getLogger(__name__)

_NULLABLE_ANYOF = (
    [{"type": "null"}, {"type": "string"}],
    [{"type": "string"}, {"type": "null"}],
    [{"type": "null"}, {"type": "integer"}],
    [{"type": "integer"}, {"type": "null"}],
    [{"type": "null"}, {"type": "boolean"}],
    [{"type": "boolean"}, {"type": "null"}],
    [{"type": "null"}, {"type": "number"}],
    [{"type": "number"}, {"type": "null"}],
)


def _to_nullable_type(item_type: str) -> dict:
    """OpenAI strict 模式的可空字段：{"type": ["string", "null"]}。"""
    return {"type": [item_type, "null"]}


def _resolve_refs(node, defs: dict) -> dict:
    """递归展开 Pydantic model_json_schema 产生的 $ref / $defs。"""
    if isinstance(node, dict):
        if "$ref" in node:
            ref_name = node["$ref"].rsplit("/", 1)[-1]
            resolved = _resolve_refs(defs.get(ref_name, {}), defs)
            # $ref 可能带 sibling 约束（极少见），合并时以定义为准
            node = dict(resolved)
        return {k: _resolve_refs(v, defs) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve_refs(item, defs) for item in node]
    return node


def _prepare_property(prop: dict) -> dict:
    """递归把 Pydantic schema 属性适配为 OpenAI strict 约束。"""
    prepared = dict(prop)
    prepared.pop("title", None)
    prepared.pop("default", None)
    any_of = prepared.get("anyOf")
    # 可空字段：anyOf 恰含一个 null 与一个标量类型（元素可能带 maxLength 等约束）
    if isinstance(any_of, list) and len(any_of) == 2:
        non_null = [e for e in any_of if e.get("type") != "null"]
        if len(non_null) == 1 and isinstance(non_null[0].get("type"), str):
            entry = non_null[0]
            nullable = {k: v for k, v in entry.items() if k != "type"}
            nullable["type"] = [entry["type"], "null"]
            prepared.pop("anyOf", None)
            prepared.update(nullable)
    if isinstance(prepared.get("type"), str) and prepared["type"] == "object":
        props = prepared.get("properties")
        if isinstance(props, dict):
            prepared["properties"] = {
                name: _prepare_property(sub) for name, sub in props.items()
            }
            prepared["required"] = sorted(props.keys())
    return prepared


def prepare_strict_schema(model: type[BaseModel]) -> dict:
    """把 Pydantic 模型转为 OpenAI Structured Output 的 json_schema 参数。

    OpenAI strict 模式要求：所有属性进入 required、可空字段用类型数组表示、
    无 default 值、嵌套模型必须内联（展开 $ref）。此函数可直接单元测试（无需网络）。
    """
    schema = model.model_json_schema()
    defs = schema.get("$defs", {})
    schema = _resolve_refs(schema, defs)
    props = schema.get("properties", {})
    prepared_props = {name: _prepare_property(prop) for name, prop in props.items()}
    return {
        "name": model.__name__,
        "strict": True,
        "schema": {
            "type": "object",
            "properties": prepared_props,
            "required": sorted(props.keys()),
            "additionalProperties": False,
        },
    }


def build_chat_client(
    *,
    api_key: str,
    base_url: str | None = None,
    timeout_seconds: float = 30.0,
):
    """构造 OpenAI SDK 客户端（惰性初始化，不发起任何网络请求）。"""
    import openai

    client_kwargs: dict = {"api_key": api_key, "timeout": timeout_seconds}
    if base_url:
        client_kwargs["base_url"] = base_url
    return openai.OpenAI(**client_kwargs)


def map_sdk_error(exc: Exception, *, timeout_seconds: float) -> None:
    """把 openai SDK 异常树统一映射为 LLM*Error（不泄露上游响应与 Key）。"""
    logger.warning("llm.upstream_error type=%s", type(exc).__name__)
    if type(exc).__name__ in ("APITimeoutError", "APIConnectionError"):
        raise LLMTimeoutError(
            f"LLM provider timed out after {timeout_seconds:g}s"
        ) from exc
    raise LLMUpstreamError("LLM provider returned an error") from exc


def to_llm_response(response, *, fallback_model: str, latency_ms: int) -> LLMResponse:
    """把 SDK 响应转换为 LLMResponse：model、content、token usage、latency。"""
    content = response.choices[0].message.content if response.choices else ""
    usage = getattr(response, "usage", None)
    return LLMResponse(
        text=content or "",
        model=response.model or fallback_model,
        prompt_tokens=getattr(usage, "prompt_tokens", None),
        completion_tokens=getattr(usage, "completion_tokens", None),
        latency_ms=latency_ms,
    )


class OpenAIChatProvider(LLMProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 30.0,
    ):
        self._client = build_chat_client(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
        )
        self.model = model
        self.timeout_seconds = timeout_seconds

    def _response_format(self, response_schema: type[BaseModel]) -> dict:
        """response_schema 非空时的 response_format；子类可覆盖（如 DeepSeek JSON Mode）。"""
        return {
            "type": "json_schema",
            "json_schema": prepare_strict_schema(response_schema),
        }

    def generate(
        self,
        messages: list[ProviderMessage],
        *,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        started = time.monotonic()
        request_kwargs: dict = {"model": self.model, "messages": messages}
        if response_schema is not None:
            request_kwargs["response_format"] = self._response_format(response_schema)
        try:
            response = self._client.chat.completions.create(**request_kwargs)
        except Exception as exc:  # noqa: BLE001 - 统一映射 SDK 异常树
            map_sdk_error(exc, timeout_seconds=self.timeout_seconds)

        latency_ms = int((time.monotonic() - started) * 1000)
        return to_llm_response(response, fallback_model=self.model, latency_ms=latency_ms)
