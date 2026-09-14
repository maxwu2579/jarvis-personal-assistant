"""Phase 8A read-only Action Proposal schemas.

The LLM emits only ``ActionProposalCandidate``.  The public proposal is built
by program code after allowlist, grounding, datetime, and future-time checks.
No model in this module contains an execution or persistence instruction.
"""

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from app.schemas.chat import UsageOut
from app.schemas.common import serialize_utc, strip_nonempty, to_utc
from app.schemas.proposal import TaskProposalRequest
from app.schemas.reminder import ReminderOut
from app.schemas.task import TaskOut
from app.schemas.execution import ExecutionOut

ActionIntent = Literal["create_task", "create_task_with_reminder", "create_calendar_event", "unsupported"]
SupportedAction = Literal["create_task", "create_task_with_reminder", "create_calendar_event"]
ProposalStatus = Literal["READY", "NEEDS_CLARIFICATION", "UNSUPPORTED", "INVALID"]


class ActionProposalRequest(TaskProposalRequest):
    """Reuse Phase 2 content and IANA-timezone validation without its write path."""


class ActionProposalCandidate(BaseModel):
    """Strict structured output requested from the existing LLM gateway.

    Temporal fields must contain the user's original phrase (not a model-made
    ISO datetime).  The service validates that grounding before parsing it.
    Nullable fields are still required so provider schemas remain explicit.
    """

    model_config = ConfigDict(extra="forbid")

    intent: ActionIntent
    task_title: str | None = Field(max_length=200)
    task_due_text: str | None = Field(max_length=200)
    reminder_time_text: str | None = Field(max_length=200)
    calendar_title: str | None = Field(default=None, max_length=200)
    calendar_start_text: str | None = Field(default=None, max_length=200)
    calendar_end_text: str | None = Field(default=None, max_length=200)
    calendar_location: str | None = Field(default=None, max_length=300)
    explanation: str = Field(max_length=500)

    @field_validator("task_title", "task_due_text", "reminder_time_text", "calendar_title",
                     "calendar_start_text", "calendar_end_text", "calendar_location")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None

    @field_validator("explanation")
    @classmethod
    def _strip_explanation(cls, value: str) -> str:
        return strip_nonempty(value, "explanation")


class ActionTaskPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    due_at: datetime | None = None

    @field_serializer("due_at")
    def _serialize_due_at(self, value: datetime | None) -> str | None:
        return serialize_utc(value)


class ActionReminderPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remind_at: datetime | None = None

    @field_serializer("remind_at")
    def _serialize_remind_at(self, value: datetime | None) -> str | None:
        return serialize_utc(value)


class ActionCalendarEventPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    start_at: datetime | None = None
    end_at: datetime | None = None
    location: str | None = Field(default=None, max_length=300)

    @field_serializer("start_at", "end_at")
    def _serialize_times(self, value: datetime | None) -> str | None:
        return serialize_utc(value)


class ActionProposalOut(BaseModel):
    """Validated read-only preview returned to the UI."""

    model_config = ConfigDict(extra="forbid")

    status: ProposalStatus
    action: SupportedAction | None
    task: ActionTaskPreview | None
    reminder: ActionReminderPreview | None
    calendar_event: ActionCalendarEventPreview | None = None
    missing_fields: list[str] = Field(default_factory=list, max_length=8)
    clarification_question: str | None = Field(default=None, max_length=300)
    explanation: str = Field(max_length=500)

    @model_validator(mode="after")
    def _validate_state_shape(self):
        if self.status == "READY":
            if self.action is None:
                raise ValueError("READY proposal requires an action")
            if self.action == "create_calendar_event":
                event = self.calendar_event
                if event is None or not event.title or event.start_at is None or event.end_at is None:
                    raise ValueError("READY calendar proposal requires title, start_at and end_at")
            elif self.task is None or not self.task.title:
                raise ValueError("READY task proposal requires a task title")
            if self.action == "create_task_with_reminder":
                if self.reminder is None or self.reminder.remind_at is None:
                    raise ValueError("READY reminder proposal requires remind_at")
        if self.status in {"UNSUPPORTED", "INVALID"} and self.action is not None:
            raise ValueError(f"{self.status} proposal cannot expose an executable action")
        if self.status == "NEEDS_CLARIFICATION":
            if not self.missing_fields or not self.clarification_question:
                raise ValueError("clarification proposal requires missing fields and a question")
        return self


class ActionProposalExchangeOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: ActionProposalOut
    usage: UsageOut


class ActionConfirmTask(BaseModel):
    """Server-revalidated Task fields accepted by the confirm endpoint."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=200)
    due_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        return strip_nonempty(value, "title")

    @field_validator("due_at")
    @classmethod
    def _normalize_due_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else to_utc(value)


class ActionConfirmReminder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    remind_at: datetime

    @field_validator("remind_at")
    @classmethod
    def _normalize_remind_at(cls, value: datetime) -> datetime:
        return to_utc(value)


class ActionConfirmCalendarEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=200)
    start_at: datetime
    end_at: datetime
    location: str | None = Field(default=None, max_length=300)

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        return strip_nonempty(value, "title")

    @field_validator("location")
    @classmethod
    def _strip_location(cls, value: str | None) -> str | None:
        return value.strip() or None if value is not None else None

    @field_validator("start_at", "end_at")
    @classmethod
    def _normalize_times(cls, value: datetime) -> datetime:
        return to_utc(value)

    @model_validator(mode="after")
    def _validate_range(self):
        if self.start_at >= self.end_at or self.end_at - self.start_at > timedelta(days=1):
            raise ValueError("calendar event must be a positive range of at most 24 hours")
        return self


class CreateTaskConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create_task"]
    task: ActionConfirmTask
    reminder: None = None


class CreateTaskWithReminderConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create_task_with_reminder"]
    task: ActionConfirmTask
    reminder: ActionConfirmReminder


class CreateCalendarEventConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["create_calendar_event"]
    task: None = None
    reminder: None = None
    calendar_event: ActionConfirmCalendarEvent


ConfirmableActionProposal = Annotated[
    CreateTaskConfirmation | CreateTaskWithReminderConfirmation | CreateCalendarEventConfirmation,
    Field(discriminator="action"),
]


class ActionProposalConfirmRequest(BaseModel):
    """The only accepted confirm body; preview metadata and handler names are forbidden."""

    model_config = ConfigDict(extra="forbid")

    proposal: ConfirmableActionProposal


class ActionProposalConfirmOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmation_id: int
    action: SupportedAction
    task: TaskOut | None
    reminder: ReminderOut | None
    execution: ExecutionOut | None = None
    replayed: bool
