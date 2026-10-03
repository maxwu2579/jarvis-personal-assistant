"""Phase 10B dedicated Memory Proposal / Confirmation orchestration.

explicit request → frozen grammar → constrained LLM candidate → strict schema
→ deterministic policy → exact-key Memory inspection → in-RAM preview
→ explicit Confirm → Memory mutation + receipt in ONE transaction.

No Execution, Worker, action.confirm, Conversation or audit copy is involved.
Generation only reads. Nothing here logs or persists the user sentence, the
candidate, preview values or raw model output.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.llm.base import LLMProvider
from app.llm.memory_prompts import build_memory_candidate_messages
from app.models.memory_receipt import MemoryConfirmationReceipt
from app.models.personal_memory import PersonalMemory
from app.schemas.memory_proposal import (
    MemoryCancelOut,
    MemoryCandidate,
    MemoryConfirmOut,
    MemoryProposalOut,
    MemoryProposalResponse,
)
from app.services.memory_grammar import (
    CANONICAL_CATEGORY,
    MAX_DAY_TOKENS,
    GrammarRejection,
    classify,
    is_canonical_value,
    normalize_days,
    same_value,
)
from app.services.memory_proposal_store import (
    MemoryProposalStore,
    PendingMemoryProposal,
    ProposalCapacityExceeded,
)
from app.services.memory_service import (
    MemoryError,
    cas_update_memory_row,
    insert_memory_row,
    is_unique_violation,
    memory_out,
)

logger = logging.getLogger(__name__)

MAX_MODEL_RESPONSE_BYTES = 8192
USER_CONFIRMED = "user_confirmed"

_CODES = {
    "READY": "MEMORY_PROPOSAL_READY",
    "ALREADY_SAVED": "MEMORY_ALREADY_SAVED",
    "NEEDS_CLARIFICATION": "MEMORY_PROPOSAL_NEEDS_CLARIFICATION",
    "UNSUPPORTED": "MEMORY_PROPOSAL_UNSUPPORTED",
    "INVALID": "MEMORY_PROPOSAL_INVALID",
}
_MESSAGES = {
    "ready": "Proposal ready. Nothing is saved until you confirm.",
    "already_saved": "This value is already saved. Nothing was changed.",
    "unsupported_expression": "This request is outside the supported Memory forms. Nothing was saved.",
    "uncertain_expression": "This request is uncertain or contains more than one fact. Please restate one supported fact.",
    "not_a_supported_form": "Please restate the request using one of the supported forms.",
    "duration_out_of_contract": "Session duration must be a whole number of minutes from 5 to 180.",
    "too_many_day_tokens": "Too many days were listed. Please list each day at most once.",
    "model_output_invalid": "The request could not be converted into a valid proposal. Nothing was saved.",
    "model_disagreement": "The model result did not match the supported input. Please generate the proposal again.",
    "existing_differs": "A different value is already saved. Use an explicit change request to replace it.",
    "existing_incompatible": "An existing memory with this key has a different format. Edit it manually instead.",
}


def _storage_error() -> MemoryError:
    return MemoryError("MEMORY_STORAGE_ERROR", "Memory storage is unavailable", 503)


def _result(status: str, reason: str, **extra) -> MemoryProposalResponse:
    logger.info("memory.proposal status=%s reason=%s", status, reason)
    return MemoryProposalResponse(status=status, code=_CODES[status], message=_MESSAGES[reason], **extra)


# ---- strict model-output parsing ----

def _reject_constant(name: str):
    raise ValueError("non-finite JSON constant")


def _no_duplicate_keys(pairs):
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def parse_candidate(text: str) -> MemoryCandidate | None:
    """Exactly one JSON object; no repair, no Markdown, no duplicates, no NaN/Infinity."""
    if not isinstance(text, str) or len(text.encode("utf-8", errors="replace")) > MAX_MODEL_RESPONSE_BYTES:
        return None
    try:
        raw = json.loads(text, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant)
    except (ValueError, TypeError, RecursionError):
        return None
    if type(raw) is not dict:
        return None
    try:
        return MemoryCandidate.model_validate(raw)
    except ValueError:
        return None


def _candidate_value(candidate: MemoryCandidate) -> object | None:
    """Enforce exactly one used value slot; None when the slot layout is inconsistent."""
    unused_time = candidate.study_time == "none"
    unused_duration = candidate.duration_minutes == 0
    unused_days = candidate.available_days == []
    if candidate.intent in ("clarify", "unsupported"):
        ok = candidate.canonical_key == "none" and unused_time and unused_duration and unused_days
        return "" if ok else None
    if candidate.canonical_key == "preferred_study_time":
        return candidate.study_time if not unused_time and unused_duration and unused_days else None
    if candidate.canonical_key == "preferred_session_duration":
        return candidate.duration_minutes if unused_time and not unused_duration and unused_days else None
    if candidate.canonical_key == "usual_available_days":
        if unused_time and unused_duration and not unused_days and len(candidate.available_days) <= MAX_DAY_TOKENS:
            return normalize_days(candidate.available_days)
        return None
    return None


# ---- generation ----

def generate_proposal(db: Session, provider: LLMProvider, store: MemoryProposalStore, clock: Clock,
                      content: str) -> MemoryProposalResponse:
    sentence = content.strip()
    verdict = classify(sentence)
    if isinstance(verdict, GrammarRejection):
        return _result(verdict.status, verdict.reason)

    # Only the frozen grammar can lead to READY; the model is a second, constrained check.
    response = provider.generate(build_memory_candidate_messages(sentence), response_schema=MemoryCandidate)
    candidate = parse_candidate(response.text)
    if candidate is None or candidate.evidence_text != sentence:
        return _result("INVALID", "model_output_invalid")
    llm_value = _candidate_value(candidate)
    if llm_value is None:
        return _result("INVALID", "model_output_invalid")
    # Astra ratification: the input is unambiguous, so a disagreeing candidate
    # (intent/key/value, or clarify/unsupported) is a model failure, not user
    # ambiguity -> INVALID. Never fall back to the grammar result, never retry.
    if (candidate.intent != verdict.intent or candidate.canonical_key != verdict.key
            or not same_value(llm_value, verdict.value)):
        return _result("INVALID", "model_disagreement")

    key, value, category = verdict.key, verdict.value, CANONICAL_CATEGORY[verdict.key]
    try:
        existing = db.scalar(select(PersonalMemory).where(PersonalMemory.key == key))
        existing_out = memory_out(existing) if existing is not None else None
        db.rollback()  # read-only: end the read transaction, write nothing
    except SQLAlchemyError:
        db.rollback()
        raise _storage_error() from None

    if existing_out is not None:
        compatible = existing_out.category == category and is_canonical_value(key, existing_out.value)
        if not compatible:
            return _result("NEEDS_CLARIFICATION", "existing_incompatible")
        if same_value(existing_out.value, value):
            return _result("ALREADY_SAVED", "already_saved", existing_memory=existing_out)
        if verdict.intent != "replace":
            return _result("NEEDS_CLARIFICATION", "existing_differs")

    now = clock.now()
    pending = PendingMemoryProposal(
        proposal_id=str(uuid.uuid4()),
        operation="UPDATE" if existing_out is not None else "CREATE",
        key=key,
        category=category,
        new_value=value,
        old_value=existing_out.value if existing_out is not None else None,
        target_memory_id=existing_out.id if existing_out is not None else None,
        expected_version=existing_out.version if existing_out is not None else None,
        created_at=now,
        expires_at=now + timedelta(seconds=store.ttl_seconds),
        expires_monotonic=store.expiry_deadline(),
    )
    try:
        store.add(pending)
    except ProposalCapacityExceeded:
        raise MemoryError("MEMORY_PROPOSAL_CAPACITY_EXCEEDED",
                          "Too many pending proposals. Try again later.", 503) from None
    return _result("READY", "ready", proposal=MemoryProposalOut(
        proposal_id=pending.proposal_id, operation=pending.operation, category=category, key=key,
        old_value=pending.old_value, new_value=value, target_memory_id=pending.target_memory_id,
        expected_version=pending.expected_version, expires_at=pending.expires_at,
    ))


# ---- confirmation / cancellation ----

def require_proposal_id(proposal_id: str) -> str:
    try:
        parsed = uuid.UUID(proposal_id)
    except (ValueError, AttributeError, TypeError):
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != proposal_id:
        raise MemoryError("VALIDATION_ERROR", "proposal_id must be a canonical UUID4", 422)
    return proposal_id


def _read_receipt(db: Session, proposal_id: str) -> MemoryConfirmOut | None:
    receipt = db.scalar(select(MemoryConfirmationReceipt).where(
        MemoryConfirmationReceipt.proposal_id == proposal_id))
    if receipt is None:
        return None
    return MemoryConfirmOut(proposal_id=receipt.proposal_id, operation=receipt.operation,
                            memory_id=receipt.memory_id, applied_version=receipt.applied_version,
                            confirmed_at=receipt.confirmed_at, replayed=True)


def _receipt_or_storage_error(db: Session, proposal_id: str) -> MemoryConfirmOut | None:
    try:
        return _read_receipt(db, proposal_id)
    except SQLAlchemyError:
        db.rollback()
        raise _storage_error() from None


def confirm_proposal(db: Session, store: MemoryProposalStore, clock: Clock,
                     proposal_id: str) -> MemoryConfirmOut:
    """Receipt first (survives expiry/restart/update/delete), then pending proposal,
    then one atomic Memory mutation + receipt. Never calls the LLM."""
    require_proposal_id(proposal_id)
    with store.lock:
        replay = _receipt_or_storage_error(db, proposal_id)
        if replay is not None:
            store.discard(proposal_id)
            return replay
        pending = store.get_valid(proposal_id)
        if pending is None:
            raise MemoryError("MEMORY_PROPOSAL_EXPIRED",
                              "This proposal expired or no longer exists. Generate a new proposal.", 410)
        now = clock.now()
        try:
            if pending.operation == "CREATE":
                try:
                    row = insert_memory_row(db, category=pending.category, key=pending.key,
                                            value=pending.new_value, source_type=USER_CONFIRMED, now=now)
                except IntegrityError as exc:
                    db.rollback()
                    if not is_unique_violation(exc):
                        raise _storage_error() from None
                    store.discard(proposal_id)
                    raise MemoryError("MEMORY_KEY_CONFLICT", "A memory with this key already exists", 409) from None
            else:
                row = cas_update_memory_row(
                    db, pending.target_memory_id, pending.expected_version, category=pending.category,
                    key=pending.key, value=pending.new_value, source_type=USER_CONFIRMED, now=now,
                    bound_category=pending.category, bound_key=pending.key,
                )
                if row is None:
                    target = db.scalar(select(PersonalMemory.id).where(PersonalMemory.id == pending.target_memory_id))
                    db.rollback()
                    store.discard(proposal_id)
                    if target is None:
                        raise MemoryError("MEMORY_TARGET_NOT_FOUND", "The memory to update no longer exists", 404)
                    raise MemoryError("MEMORY_VERSION_CONFLICT",
                                      "Memory changed since the proposal was generated. Generate a new proposal.", 409)
            memory_id, applied_version = row.id, row.version
            db.add(MemoryConfirmationReceipt(proposal_id=proposal_id, operation=pending.operation,
                                             memory_id=memory_id, applied_version=applied_version,
                                             status="APPLIED", confirmed_at=now))
            try:
                db.flush()
            except IntegrityError:
                # A concurrent confirmation committed this receipt first: undo our
                # mutation, read the committed receipt in a new transaction, and never
                # rerun the Memory mutation.
                db.rollback()
                replay = _receipt_or_storage_error(db, proposal_id)
                if replay is None:
                    raise _storage_error() from None
                store.discard(proposal_id)
                return replay
            db.commit()
        except MemoryError:
            raise
        except SQLAlchemyError:
            # Outcome uncertain: keep the same proposal ID so Confirm can be retried
            # (retry always checks the receipt first).
            db.rollback()
            raise _storage_error() from None
        store.discard(proposal_id)
        logger.info("memory.proposal.confirmed operation=%s", pending.operation)
        return MemoryConfirmOut(proposal_id=proposal_id, operation=pending.operation, memory_id=memory_id,
                                applied_version=applied_version, confirmed_at=now, replayed=False)


def cancel_proposal(db: Session, store: MemoryProposalStore, proposal_id: str) -> MemoryConfirmOut | MemoryCancelOut:
    """Idempotent. Already APPLIED → historical result; never deletes a Memory."""
    require_proposal_id(proposal_id)
    with store.lock:
        replay = _receipt_or_storage_error(db, proposal_id)
        if replay is not None:
            store.discard(proposal_id)
            return replay
        store.discard(proposal_id)
        return MemoryCancelOut(proposal_id=proposal_id)
