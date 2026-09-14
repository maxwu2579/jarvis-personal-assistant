"""Reminder 与 Notification 数据模型。

状态机：
PENDING → CLAIMED → DELIVERED
   │          │
   └─ CANCELLED（任务完成时取消未投递的提醒）
   └─ CLAIMED 投递失败重试至上限 → FAILED

并发与幂等设计：
- Reminder(status, remind_at) 索引支持 Worker 轮询；
- 活动提醒部分唯一索引：同一 Task 最多一个 PENDING/CLAIMED 的 Reminder；
- Notification.reminder_id 唯一约束：一个 Reminder 最多一条通知（最终防重保障）；
- 数据库是 Reminder 状态的唯一事实来源（无内存 Timer）。
所有时间统一 UTC。
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.task import utcnow


class ReminderStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class NotificationType(str, Enum):
    REMINDER_DUE = "REMINDER_DUE"


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ReminderStatus.PENDING.value, index=True
    )
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        # Worker 轮询：PENDING 且到期的提醒
        Index("ix_reminders_status_remind_at", "status", "remind_at"),
        # 活动提醒（PENDING/CLAIMED）部分唯一索引：同一 Task 最多一个未结束提醒。
        # SQLite 与 PostgreSQL 都支持 partial unique index（方言参数各自生效）：
        # 这是「重复 confirm 不创建第二个提醒」的最终防重。Phase 5A：为 PG 补充
        # postgresql_where，否则 PG 上会编译成全量唯一索引（语义变化——已投递
        # 的提醒历史会导致同一任务无法再次创建提醒）。
        Index(
            "uq_reminders_active_task",
            "task_id",
            unique=True,
            sqlite_where=text("status IN ('PENDING', 'CLAIMED')"),
            postgresql_where=text("status IN ('PENDING', 'CLAIMED')"),
        ),
        {"sqlite_autoincrement": True},
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # 唯一约束：一个 Reminder 最多一条通知（幂等投递的最终保障）
    reminder_id: Mapped[int] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False, unique=True, index=True
    )
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[str] = mapped_column(
        String(30), nullable=False, default=NotificationType.REMINDER_DUE.value
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = ({"sqlite_autoincrement": True},)
