"""对话 / 消息的请求与响应 schema。extra 字段一律拒绝。"""

from datetime import datetime

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


class MessageCreate(BaseModel):
    """发送消息：content 去除首尾空格后必须非空。"""

    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=CONTENT_MAX)

    @field_validator("content")
    @classmethod
    def _strip_content(cls, value: str) -> str:
        return strip_nonempty(value, "content")


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

    @field_serializer("created_at")
    def _ser_utc(self, value: datetime) -> str:
        return serialize_utc(value)


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
