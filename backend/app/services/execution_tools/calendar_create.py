"""Strict, static tool adapter for one confirmed Outlook event creation."""

from datetime import datetime, timedelta, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.clock import get_clock
from app.integrations.calendar.errors import CalendarError
from app.integrations.calendar.factory import get_calendar_provider
from app.schemas.common import serialize_utc


class CalendarCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    start_at: str
    end_at: str
    location: str | None = Field(default=None, max_length=300)
    transaction_id: str

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title is empty")
        return value.strip()

    @field_validator("transaction_id")
    @classmethod
    def valid_transaction_id(cls, value: str) -> str:
        if str(UUID(value)) != value.lower():
            raise ValueError("transaction_id must be a canonical UUID")
        return value

    @staticmethod
    def _time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc)

    @model_validator(mode="after")
    def valid_range(self):
        start, end = self._time(self.start_at), self._time(self.end_at)
        if not timedelta(0) < end - start <= timedelta(days=1):
            raise ValueError("invalid calendar range")
        return self


class CalendarCreateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_event_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=200)
    start_at: str
    end_at: str
    location: str | None = Field(default=None, max_length=300)


def calendar_create_event_handler(payload: dict) -> dict:
    try:
        value = CalendarCreateInput.model_validate(payload, strict=True)
        if value._time(value.start_at) <= get_clock().now().astimezone(timezone.utc):
            raise CalendarError("CALENDAR_EVENT_TIME_PASSED")
        result = get_calendar_provider().create_event(
            title=value.title,
            start=value._time(value.start_at),
            end=value._time(value.end_at),
            location=value.location,
            transaction_id=value.transaction_id,
        )
        return CalendarCreateOutput(
            external_event_id=result.external_event_id,
            title=result.title,
            start_at=serialize_utc(result.start_at),
            end_at=serialize_utc(result.end_at),
            location=result.location,
        ).model_dump()
    except CalendarError:
        raise
    except Exception:
        raise CalendarError("CALENDAR_CREATE_INVALID") from None
