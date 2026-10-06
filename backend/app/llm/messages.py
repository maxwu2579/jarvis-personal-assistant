"""把数据库消息记录组装为 Provider 消息格式（OpenAI 兼容）。

Phase 10C 隐私不变量（普通 Chat / 任务建议 / 行动提案共用本模块）：
只有「来源标记被证明为合法存储的 false」的历史行才能进入 Provider 历史。
true、NULL、缺失、无法识别或损坏的原始存储一律排除（fail closed）。
"""

import logging

from app.llm.base import LLMUpstreamError, ProviderMessage
from app.models.conversation import Message, MessageRole

logger = logging.getLogger(__name__)


def role_to_provider(role: MessageRole) -> str:
    if role in (MessageRole.USER, MessageRole.ASSISTANT):
        return role.value.lower()
    if role in (MessageRole.SYSTEM, MessageRole.TOOL):
        # 工具消息在真实工具接入前按 system 语义传递（接入工具时改为 tool + tool_call_id）
        logger.warning("llm.role_map role=%s -> system", role.value)
        return "system"
    raise LLMUpstreamError(f"unsupported message role {role.value!r}")


def provenance_proven_false(message: Message) -> bool:
    """True 仅当两项同时成立：解码后的标记恰为 Python False，且数据库派生的
    有效性属性确认底层存储是合法 false。单凭 ORM 解码出的 False 不足为证。"""
    return (
        getattr(message, "memory_context_applied", None) is False
        and getattr(message, "memory_context_storage_valid_false", None) is True
    )


def build_provider_messages(history: list[Message], *, system_prompt: str | None = None) -> list[ProviderMessage]:
    """历史消息 → Provider 消息列表；system_prompt 非空时放在最前。

    来源标记未被证明为合法 false 的行不进入结果（与角色无关），其余保持原顺序。
    """
    messages: list[ProviderMessage] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(
        {"role": role_to_provider(MessageRole(m.role)), "content": m.content}
        for m in history
        if provenance_proven_false(m)
    )
    return messages
