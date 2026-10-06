"""Conversation 与 Message 数据模型。所有时间统一 UTC。"""

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, false
from sqlalchemy.orm import Mapped, column_property, mapped_column

from app.core.database import Base
from app.models.task import utcnow


class MessageRole(str, Enum):
    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"
    TOOL = "TOOL"


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    # Phase 10C：仅「普通 Chat 在成功提供非空已保存偏好后生成的 ASSISTANT 回复」为 true。
    # 被标记的回复永不再进入任何 Provider 历史（见 app/llm/messages.py）。
    memory_context_applied: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    # 内部只读派生属性（不是第二个列，不进入 API / 前端 / Provider）：由数据库按
    # 「等于方言 false」计算，证明底层存储是合法的 false。ORM 解码出的 Python False
    # 不足为证：SQLite 中损坏的原始值（空文本 / 空 blob）也会被解码为 False。
    # 合法 false → True；true / 损坏值 → False；NULL → None。
    memory_context_storage_valid_false: Mapped[bool | None] = column_property(
        memory_context_applied == false()
    )

    __table_args__ = ({"sqlite_autoincrement": True},)
