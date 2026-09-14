"""Reminder / Notification 的响应 schema。只读接口，无创建请求。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_serializer

from app.models.reminder import NotificationType, ReminderStatus
from app.schemas.common import serialize_utc


class ReminderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    task_id: int
    status: ReminderStatus
    remind_at: datetime
    claimed_at: datetime | None
    lease_expires_at: datetime | None
    delivered_at: datetime | None
    attempt_count: int
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime

    @field_serializer(
        "remind_at", "claimed_at", "lease_expires_at", "delivered_at", "created_at", "updated_at"
    )
    def _ser_utc(self, value: datetime | None) -> str | None:
        return serialize_utc(value)


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    reminder_id: int
    task_id: int
    type: NotificationType
    title: str
    body: str
    created_at: datetime
    read_at: datetime | None

    @field_serializer("created_at", "read_at")
    def _ser_utc(self, value: datetime | None) -> str | None:
        return serialize_utc(value)


class UnreadCountOut(BaseModel):
    count: int
