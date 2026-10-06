"""对话 / 消息的请求与响应 schema。extra 字段一律拒绝。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.models.conversation import MessageRole
from app.schemas.common import serialize_utc, strip_nonempty

TITLE_MAX = 200
CONTENT_MAX = 8000


class ConversationCreate(BaseModel):
    """创建对话：title 可选，去除首尾空格后为空视为无标题。"""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=TITLE_MAX)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


MemoryContextKey = Literal[
    "preferred_study_time", "preferred_session_duration", "usual_available_days"
]
MemoryContextStatus = Literal["disabled", "applied", "empty", "unavailable"]
MEMORY_KEYS_MAX = 3


class MessageCreate(BaseModel):
    """发送消息：content 去除首尾空格后必须非空。

    memory_keys（Phase 10C）：本次发送显式选择的已保存偏好 key，0–3 个、互不重复、
    只能是三个 canonical key。客户端不能提供偏好值、来源标记、Provider 或行 ID。
    """

    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=CONTENT_MAX)
    memory_keys: list[MemoryContextKey] = Field(default_factory=list, max_length=MEMORY_KEYS_MAX)

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        return strip_nonempty(value, "content")

    @field_validator("memory_keys")
    @classmethod
    def _unique_memory_keys(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("memory_keys must not contain duplicates")
        return value


class ConversationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _ser_utc(self, value: datetime) -> str:
        return serialize_utc(value)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    conversation_id: int
    role: MessageRole
    content: str
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None
    created_at: datetime
    # Phase 10C：持久来源标记；仅此布尔值，不含历史 key / value。
    memory_context_applied: bool

    @field_serializer("created_at")
    def _ser_utc(self, value: datetime) -> str:
        return serialize_utc(value)


class MemoryContextOut(BaseModel):
    """本次发送的 Memory 上下文结果（瞬时元数据，不落库）：仅 status 与实际包含的 key。"""

    status: MemoryContextStatus
    keys: list[MemoryContextKey]


class UsageOut(BaseModel):
    """一次模型调用的基础 metadata（不含模型名，模型名在 assistant_message 上）。"""

    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None


class ChatExchangeOut(BaseModel):
    """发送消息的完整响应：两条落库消息 + 调用 metadata。"""

    user_message: MessageOut
    assistant_message: MessageOut
    usage: UsageOut
    memory_context: MemoryContextOut
