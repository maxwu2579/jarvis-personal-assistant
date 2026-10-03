"""Phase 10B Memory Proposal / Confirmation contracts.

The client supplies only a sentence (generation) or nothing (confirm/cancel).
IDs, versions, category, source_type, operation and expiry are server-owned.
"""

import unicodedata
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from app.schemas.common import serialize_utc
from app.schemas.memory import MemoryCategory, MemoryOut

MAX_CONTENT_CHARS = 1000
MAX_CONTENT_BYTES = 4096


class MemoryProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str

    @field_validator("content")
    @classmethod
    def bounded_single_line(cls, value: str) -> str:
        if len(value.encode("utf-8", errors="strict")) > MAX_CONTENT_BYTES:
            raise ValueError("content must not exceed 4096 UTF-8 bytes")
        if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in value):
            raise ValueError("content must be a single line without control characters")
        if not 1 <= len(value.strip()) <= MAX_CONTENT_CHARS:
            raise ValueError("content must contain 1-1000 characters after trimming")
        return value


class MemoryConfirmRequest(BaseModel):
    """Confirm takes an empty object: value, version, operation and source are not client inputs."""

    model_config = ConfigDict(extra="forbid", strict=True)


CandidateIntent = Literal["remember", "replace", "clarify", "unsupported"]
CandidateKey = Literal["preferred_study_time", "preferred_session_duration", "usual_available_days", "none"]
CandidateStudyTime = Literal["morning", "afternoon", "evening", "none"]
CandidateWeekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class MemoryCandidate(BaseModel):
    """Flat LLM candidate; every field required. It never carries CREATE/UPDATE,
    IDs, versions, category, source_type or confirmation state."""

    model_config = ConfigDict(extra="forbid", strict=True)
    intent: CandidateIntent
    canonical_key: CandidateKey
    study_time: CandidateStudyTime
    duration_minutes: int
    available_days: list[CandidateWeekday]
    evidence_text: str


ProposalStatus = Literal["READY", "ALREADY_SAVED", "NEEDS_CLARIFICATION", "UNSUPPORTED", "INVALID"]
ProposalOperation = Literal["CREATE", "UPDATE"]
CanonicalValue = str | int | list[str]


class MemoryProposalOut(BaseModel):
    proposal_id: str
    operation: ProposalOperation
    category: MemoryCategory
    key: str
    old_value: CanonicalValue | None
    new_value: CanonicalValue
    target_memory_id: int | None
    expected_version: int | None
    source_type: Literal["user_confirmed"] = "user_confirmed"
    expires_at: datetime

    @field_serializer("expires_at")
    def _expires(self, value: datetime) -> str:
        return serialize_utc(value)


class MemoryProposalResponse(BaseModel):
    status: ProposalStatus
    code: str
    message: str
    proposal: MemoryProposalOut | None = None
    existing_memory: MemoryOut | None = None


class MemoryConfirmOut(BaseModel):
    status: Literal["APPLIED"] = "APPLIED"
    proposal_id: str
    operation: ProposalOperation
    memory_id: int
    applied_version: int
    confirmed_at: datetime
    replayed: bool

    @field_serializer("confirmed_at")
    def _confirmed(self, value: datetime) -> str:
        return serialize_utc(value)


class MemoryCancelOut(BaseModel):
    status: Literal["CANCELLED"] = "CANCELLED"
    proposal_id: str
