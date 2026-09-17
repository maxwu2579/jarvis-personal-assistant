"""DeepSeek Provider：官方 API（OpenAI 兼容协议），复用 OpenAIChatProvider 的实现。

与 OpenAI Provider 的唯一差异是 Structured Output 的方式：
- DeepSeek 不支持 OpenAI strict json_schema（response_format={"type":"json_schema"}）；
- response_schema 非空时改用 JSON Mode：response_format={"type":"json_object"}，
  并在请求最前追加一条 system 消息，明确要求只返回 JSON 且结果必须符合给定
  schema（DeepSeek 官方要求 prompt 中出现 json 字样，否则 JSON Mode 可能不生效）；
- schema 摘要只含字段名与类型（不含任何密钥），有固定长度上限，超长截断；
- 返回内容仍由调用方（proposal_service）用 Pydantic 二次验证，失败走现有错误映射，
  不产生 Task、不伪造 ASSISTANT、不绕过 REJECTED 审计。

客户端初始化、超时、SDK 异常映射与 usage 解析全部复用 openai_provider 的共享辅助。
"""

from pydantic import BaseModel

from app.llm.base import LLMResponse, ProviderMessage
from app.llm.openai_provider import OpenAIChatProvider

DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"

# JSON Mode 指令中 schema 摘要的固定长度上限（防止请求体膨胀；内容不含密钥）
_JSON_MODE_MAX_SCHEMA_CHARS = 1000


def _field_type(prop: dict) -> str:
    """把 schema 属性的类型压缩为简短字符串（type / const / anyOf 变体）。"""
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        return "enum:" + "|".join(str(value) for value in enum)
    prop_type = prop.get("type")
    if isinstance(prop_type, str):
        return prop_type
    if prop.get("const") is not None:
        return f"const:{prop['const']}"
    any_of = prop.get("anyOf")
    if isinstance(any_of, list):
        variants = []
        for item in any_of:
            item_enum = item.get("enum")
            if isinstance(item_enum, list) and item_enum:
                variants.append("enum:" + "|".join(str(value) for value in item_enum))
                continue
            item_type = item.get("type")
            if isinstance(item_type, str):
                variants.append(item_type)
            elif item.get("const") is not None:
                variants.append(f"const:{item['const']}")
            elif item_type is None:
                variants.append("any")
        if variants:
            return "|".join(dict.fromkeys(variants))  # 去重保序
    return "any"


def build_json_mode_system_message(response_schema: type[BaseModel]) -> dict:
    """构造 JSON Mode 的 system 指令：只返回 JSON，且必须符合给定 schema。

    只携带对象名与字段名/类型摘要（model_json_schema 不含密钥）；超长截断并明示。
    """
    schema = response_schema.model_json_schema()
    fields = {
        name: _field_type(prop)
        for name, prop in schema.get("properties", {}).items()
    }
    summary = "; ".join(f"{name}: {field_type}" for name, field_type in fields.items())
    if len(summary) > _JSON_MODE_MAX_SCHEMA_CHARS:
        summary = summary[: _JSON_MODE_MAX_SCHEMA_CHARS] + "...(truncated)"
    object_name = schema.get("title") or response_schema.__name__
    return {
        "role": "system",
        "content": (
            f"JSON mode. Return the target data object itself as one valid {object_name} "
            "JSON object; no markdown or commentary. "
            "Return the target data object itself, never a schema descriptor or metadata object. "
            "Do not return wrapper keys such as object, fields, schema, or properties. "
            "Use exactly the top-level fields below, include every field, use null for a "
            "missing nullable value, and follow the declared types/enums: " + summary
        ),
    }


class DeepSeekChatProvider(OpenAIChatProvider):
    """DeepSeek 官方 API Provider（OpenAI 兼容）。

    普通对话与 OpenAI 行为完全一致（不发 response_format）；仅当调用方传入
    response_schema 时启用 JSON Mode 并注入显式 JSON 指令。
    """

    def _response_format(self, response_schema: type[BaseModel]) -> dict:
        """DeepSeek 使用 JSON Mode，而非 OpenAI strict json_schema。"""
        return {"type": "json_object"}

    def generate(
        self,
        messages: list[ProviderMessage],
        *,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        if response_schema is not None:
            messages = [build_json_mode_system_message(response_schema), *messages]
        return super().generate(messages, response_schema=response_schema)
