"""把数据库消息记录组装为 Provider 消息格式（OpenAI 兼容）。"""

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


def build_provider_messages(history: list[Message], *, system_prompt: str | None = None) -> list[ProviderMessage]:
    """历史消息 → Provider 消息列表；system_prompt 非空时放在最前。"""
    messages: list[ProviderMessage] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.extend(
        {"role": role_to_provider(MessageRole(m.role)), "content": m.content}
        for m in history
    )
    return messages
