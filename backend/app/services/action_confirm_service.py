"""Phase 8B explicit confirmation with atomic domain writes and durable replay.

The existing Execution table is used only as an internal idempotency receipt.
The receipt is completed synchronously in the same transaction as Task and
Reminder creation.  It is never queued, claimed, stepped, or dispatched to a
tool handler.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timezone
from uuid import uuid4

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.database import DIALECT
from app.core.dialect import coerce_utc_for_db
from app.models.execution import Execution, ExecutionStatus
from app.models.execution_event import ExecutionEvent
from app.models.reminder import Reminder
from app.models.task import Task
from app.integrations.calendar.factory import get_calendar_auth
from app.schemas.action_proposal import ConfirmableActionProposal
from app.schemas.common import serialize_utc
from app.services import execution_service, reminder_service, task_service
from app.services.execution_steps import grant_confirmation
from app.services.chat_service import get_conversation
from app.services.execution_errors import (
    ActionConfirmResultMissingError,
    ActionConfirmTimeError,
)
from app.services.execution_events import append_event


RECEIPT_EXECUTION_TYPE = "action.confirm"


@dataclass(slots=True)
class ActionConfirmationResult:
    confirmation_id: int
    action: str
    task: Task | None
    reminder: Reminder | None
    execution: Execution | None
    replayed: bool


def _canonical_payload(conversation_id: int, proposal: ConfirmableActionProposal) -> dict:
    return {
        "conversation_id": conversation_id,
        "action": proposal.action,
        "task": ({
            "title": proposal.task.title,
            "due_at": serialize_utc(proposal.task.due_at),
        } if proposal.action != "create_calendar_event" else None),
        "reminder": (
            {"remind_at": serialize_utc(proposal.reminder.remind_at)}
            if proposal.action == "create_task_with_reminder"
            else None
        ),
        "calendar_event": ({
            "title": proposal.calendar_event.title,
            "start_at": serialize_utc(proposal.calendar_event.start_at),
            "end_at": serialize_utc(proposal.calendar_event.end_at),
            "location": proposal.calendar_event.location,
        } if proposal.action == "create_calendar_event" else None),
    }


def _read_committed_result(db: Session, receipt: Execution) -> ActionConfirmationResult:
    if receipt.status != ExecutionStatus.SUCCEEDED.value:
        raise ActionConfirmResultMissingError()
    event = (
        db.query(ExecutionEvent)
        .filter(
            ExecutionEvent.execution_id == receipt.id,
            ExecutionEvent.event_type == "EXECUTION_SUCCEEDED",
        )
        .order_by(ExecutionEvent.sequence_number.desc())
        .first()
    )
    try:
        result = json.loads(event.payload) if event is not None and event.payload else None
        stored_action = json.loads(receipt.payload)["action"]
        if stored_action == "create_calendar_event":
            execution_id = result["execution_id"]
            if type(execution_id) is not int:
                raise ValueError()
            execution = db.get(Execution, execution_id)
            if execution is None or execution.execution_type != "calendar.create_event":
                raise ValueError()
            return ActionConfirmationResult(
                confirmation_id=receipt.id, action=stored_action, task=None,
                reminder=None, execution=execution, replayed=True,
            )
        task_id = result["task_id"]
        reminder_id = result["reminder_id"]
    except (KeyError, TypeError, ValueError):
        raise ActionConfirmResultMissingError() from None
    if type(task_id) is not int or (reminder_id is not None and type(reminder_id) is not int):
        raise ActionConfirmResultMissingError()

    task = db.get(Task, task_id)
    reminder = db.get(Reminder, reminder_id) if reminder_id is not None else None
    if task is None or (reminder_id is not None and reminder is None):
        raise ActionConfirmResultMissingError()
    if reminder is not None and reminder.task_id != task.id:
        raise ActionConfirmResultMissingError()
    if stored_action not in {"create_task", "create_task_with_reminder"}:
        raise ActionConfirmResultMissingError()
    return ActionConfirmationResult(
        confirmation_id=receipt.id,
        action=stored_action,
        task=task,
        reminder=reminder,
        execution=None,
        replayed=True,
    )


def _validate_fresh_times(proposal: ConfirmableActionProposal, *, clock: Clock) -> None:
    now_utc = clock.now().astimezone(timezone.utc)
    if proposal.action == "create_calendar_event":
        if proposal.calendar_event.start_at <= now_utc:
            raise ActionConfirmTimeError("calendar_event.start_at")
        if proposal.calendar_event.end_at <= proposal.calendar_event.start_at:
            raise ActionConfirmTimeError("calendar_event.end_at")
        return
    if proposal.task.due_at is not None and proposal.task.due_at <= now_utc:
        raise ActionConfirmTimeError("task.due_at")
    if (
        proposal.action == "create_task_with_reminder"
        and proposal.reminder.remind_at <= now_utc
    ):
        raise ActionConfirmTimeError("reminder.remind_at")


def confirm_action_proposal(
    db: Session,
    *,
    conversation_id: int,
    proposal: ConfirmableActionProposal,
    idempotency_key: str,
    correlation_id: str | None,
    clock: Clock,
) -> ActionConfirmationResult:
    """Confirm one typed action; all new rows commit or roll back together."""
    get_conversation(db, conversation_id)
    payload = _canonical_payload(conversation_id, proposal)

    try:
        receipt = execution_service.create_execution(
            db,
            execution_type=RECEIPT_EXECUTION_TYPE,
            payload=payload,
            idempotency_key=idempotency_key,
            owner_id=f"conversation:{conversation_id}",
            correlation_id=correlation_id,
            clock=clock,
            commit=False,
            allow_internal_type=True,
        )
        if bool(getattr(receipt, "_replayed", False)):
            return _read_committed_result(db, receipt)

        if proposal.action == "create_calendar_event":
            # New confirmations require write consent. A replay only returns
            # the already committed execution, even if permission was lost.
            # No Graph call occurs in this transaction.
            get_calendar_auth().write_access_token()
        _validate_fresh_times(proposal, clock=clock)
        task = None
        reminder = None
        calendar_execution = None
        if proposal.action == "create_calendar_event":
            transaction_id = str(uuid4())
            event = proposal.calendar_event
            calendar_execution = execution_service.create_execution(
                db,
                execution_type="calendar.create_event",
                payload={
                    "title": event.title,
                    "start_at": serialize_utc(event.start_at),
                    "end_at": serialize_utc(event.end_at),
                    "location": event.location,
                    "transaction_id": transaction_id,
                },
                idempotency_key=f"calendar.action.{receipt.id}",
                correlation_id=correlation_id,
                clock=clock,
                commit=False,
            )
            execution_service.enqueue(db, calendar_execution.id, clock=clock, commit=False)
            grant_confirmation(
                db, calendar_execution.id, "create_event",
                actor_type="user", actor_id=f"conversation:{conversation_id}",
                clock=clock, commit=False,
            )
        else:
            task = task_service.create_task(
                db,
                title=proposal.task.title,
                description=None,
                due_at=proposal.task.due_at,
                commit=False,
            )
            db.flush()
        if proposal.action == "create_task_with_reminder":
            reminder = reminder_service.create_for_action_confirmation(
                db,
                task=task,
                remind_at=proposal.reminder.remind_at,
            )
            db.flush()

        now = coerce_utc_for_db(clock.now(), DIALECT)
        receipt.status = ExecutionStatus.SUCCEEDED.value
        receipt.started_at = now
        receipt.finished_at = now
        append_event(
            db,
            execution_id=receipt.id,
            event_type="EXECUTION_SUCCEEDED",
            actor_type="user",
            actor_id="action_confirm",
            correlation_id=correlation_id,
            payload=(
                {"execution_id": calendar_execution.id}
                if calendar_execution is not None
                else {
                    "task_id": task.id if task is not None else None,
                    "reminder_id": reminder.id if reminder is not None else None,
                }
            ),
            clock=clock,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(receipt)
    if task is not None:
        db.refresh(task)
    if reminder is not None:
        db.refresh(reminder)
    return ActionConfirmationResult(
        confirmation_id=receipt.id,
        action=proposal.action,
        task=task,
        reminder=reminder,
        execution=calendar_execution,
        replayed=False,
    )
