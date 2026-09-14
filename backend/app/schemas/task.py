"""Task 的请求 / 响应 schema。

时区规则：due_at 必须是带时区的 ISO 8601 字符串。收到 naive 时间直接报 422，
绝不静默假设时区。合法输入统一转换为 UTC 后落库。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from app.models.task import TaskStatus
from app.schemas.common import serialize_utc, strip_nonempty, to_utc


class TaskCreate(BaseModel):
    """创建请求：不允许携带 status / id / created_at / updated_at，一律生成 DRAFT。

    extra="forbid" 确保客户端无法通过多传字段（如 status=DONE、id=5）绕过约束，
    未定义字段一律 422，绝不静默忽略。
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    due_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        return strip_nonempty(value, "title")

    @field_validator("description")
    @classmethod
    def _strip_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    @field_validator("due_at")
    @classmethod
    def _validate_due_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return to_utc(value)


class TaskPostpone(BaseModel):
    """延期请求：只允许 CONFIRMED 任务调用，且必须提供新的带时区 due_at。

    extra="forbid"：请求体只能包含 due_at 一个字段。
    """

    model_config = ConfigDict(extra="forbid")

    due_at: datetime

    @field_validator("due_at")
    @classmethod
    def _validate_due_at(cls, value: datetime) -> datetime:
        return to_utc(value)


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    description: str | None
    status: TaskStatus
    due_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @field_serializer("due_at", "created_at", "updated_at")
    def _ser_utc(self, value: datetime | None) -> str | None:
        return serialize_utc(value)
