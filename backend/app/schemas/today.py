"""Phase 11A Today 聚合投影的响应 schema（只读；字段集合固定，不含任何额外字段）。

- 所有时间输出为 UTC ISO 8601（'Z' 结尾），与其他域一致，经 serialize_utc；
- section 不可读时 status="unavailable"，其余字段一律为 null（绝不返回空列表或 0）；
- 不含任务描述、提醒租约 / 重试字段、执行 payload / 错误 / attempt 字段，也不含任何日历数据。
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer

from app.schemas.common import serialize_utc

SectionStatus = Literal["ok", "unavailable"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TodayTask(_Model):
    id: int
    title: str
    status: Literal["DRAFT", "CONFIRMED"]
    due_at: datetime | None

    @field_serializer("due_at")
    def _ser(self, value: datetime | None) -> str | None:
        return serialize_utc(value)


class TaskList(_Model):
    items: list[TodayTask]
    total: int
    truncated: bool


class OtherOpenTasks(_Model):
    later: int
    no_due_date: int


class TaskSection(_Model):
    status: SectionStatus
    drafts: TaskList | None = None
    overdue: TaskList | None = None
    due_today: TaskList | None = None
    other_open: OtherOpenTasks | None = None


class TodayReminder(_Model):
    id: int
    task_id: int
    task_title: str
    status: Literal["PENDING", "CLAIMED", "DELIVERED", "FAILED"]
    remind_at: datetime

    @field_serializer("remind_at")
    def _ser(self, value: datetime) -> str | None:
        return serialize_utc(value)


class ReminderList(_Model):
    items: list[TodayReminder]
    total: int
    truncated: bool


class ReminderSection(_Model):
    status: SectionStatus
    scheduled: ReminderList | None = None
    delivered: ReminderList | None = None
    attention: ReminderList | None = None


class TodayNotification(_Model):
    id: int
    task_id: int
    title: str
    body: str
    created_at: datetime

    @field_serializer("created_at")
    def _ser(self, value: datetime) -> str | None:
        return serialize_utc(value)


class NotificationList(_Model):
    items: list[TodayNotification]
    total: int
    truncated: bool


class NotificationSection(_Model):
    status: SectionStatus
    unread: NotificationList | None = None


class TodayExecution(_Model):
    id: int
    execution_type: str
    status: str
    reason: Literal["confirmation_required", "failed", "timed_out"]
    occurred_at: datetime

    @field_serializer("occurred_at")
    def _ser(self, value: datetime) -> str | None:
        return serialize_utc(value)


class ExecutionList(_Model):
    items: list[TodayExecution]
    total: int
    truncated: bool


class ExecutionSection(_Model):
    status: SectionStatus
    attention: ExecutionList | None = None


class TodayResponse(_Model):
    timezone: str
    local_date: date
    generated_at: datetime
    day_start: datetime
    day_end: datetime
    tasks: TaskSection
    reminders: ReminderSection
    notifications: NotificationSection
    executions: ExecutionSection

    @field_serializer("generated_at", "day_start", "day_end")
    def _ser(self, value: datetime) -> str | None:
        return serialize_utc(value)
