"""TaskProposal 审计模型：记录「哪条消息 → 什么建议 → 哪个 DRAFT 任务」。

状态语义：
- SUCCEEDED：建议验证通过并创建了 DRAFT 任务（task_id 必填）；
- REJECTED：模型提出了建议但未通过校验（不创建任务、不创建伪造的
  ASSISTANT 消息；assistant_message_id/task_id 为空；error_code/error_detail
  记录可审计的失败原因，不保存堆栈、API Key 或完整原始供应商响应）。

边界：Provider 层失败（timeout / upstream error）发生在「建议被提出」之前，
不落 Proposal 审计——只保留触发它的 USER 消息（见 README「失败审计边界」）。
"""

import json
from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.task import utcnow


class TaskProposalStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"


class TaskProposal(Base):
    __tablename__ = "task_proposals"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_message_id: Mapped[int] = mapped_column(
        ForeignKey("messages.id"), nullable=False
    )
    # REJECTED 时没有伪造的 ASSISTANT 成功消息，因此可空
    assistant_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True
    )
    # SUCCEEDED 时恰好对应一个任务；REJECTED 时为空。
    # unique 约束在 SQLite 下允许多个 NULL（多个 REJECTED 记录互不冲突）
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id"), nullable=True, unique=True, index=True
    )
    # 模型提议的 action（校验失败时可能是非法值，仅审计用途，不用于执行）
    action: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 验证通过后的规范化参数（SUCCEEDED 时必填；REJECTED 时不保存原始输出）
    arguments_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=TaskProposalStatus.SUCCEEDED.value,
        index=True,
    )
    # 审计用稳定错误码与简述（不含敏感数据）
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    @property
    def arguments(self) -> dict | None:
        """解析后的规范化参数（供 TaskProposalOut 以 from_attributes 读取）。"""
        if not self.arguments_json:
            return None
        return json.loads(self.arguments_json)

    __table_args__ = ({"sqlite_autoincrement": True},)
