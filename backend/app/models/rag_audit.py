"""RAG 回答审计表（Phase 4C）：每条 /api/rag/ask 的结构化可追溯记录。

设计决策：
- 每条 ask 都写一条记录（含独立使用、无 conversation 的场景）；
- 结构化 JSON 列（document_ids / chunk_ids / citation_labels）可查询，
  不把引用塞进无法检索的自由文本；
- conversation/user/assistant 三个消息 id 可空：独立使用与拒答路径
  （固定文案回答无模型调用）都有确定表示；
- 不存完整 system prompt、API Key、原始异常、模型原始输出；
- status：ANSWERED / INSUFFICIENT_EVIDENCE / FAILED（FAILED 记录
  error_code，如 LLM_TIMEOUT / RAG_RESPONSE_INVALID / RAG_CITATION_INVALID）。
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.task import utcnow


class RagAnswerStatus(str, Enum):
    ANSWERED = "ANSWERED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    FAILED = "FAILED"


class RagAnswer(Base):
    __tablename__ = "rag_answers"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # 可空：RAG 允许独立使用（不强制 conversation）
    conversation_id: Mapped[int | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assistant_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 用户问题（用户数据，非敏感配置）；不存完整 system prompt
    question: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    retrieval_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    # 结构化快照（JSON 数组文本）：本次检索/下发的文档与块、最终引用标签
    document_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_ids_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    citation_labels_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    returned_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_chars: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    context_token_estimate: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = ({"sqlite_autoincrement": True},)
