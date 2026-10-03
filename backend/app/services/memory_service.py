"""Explicit CRUD only. No integration with LLMs, workers or other domains.

insert_memory_row / cas_update_memory_row validate, execute SQL and flush but
never commit or roll back. Manual CRUD wrappers below own their transactions;
the Phase 10B confirmation service owns Memory mutation + receipt as one.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.models.personal_memory import PersonalMemory
from app.schemas.memory import MemoryCreate, MemoryOut, MemoryUpdate


class MemoryError(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(code)
        self.code, self.message, self.status = code, message, status


def memory_out(row: PersonalMemory) -> MemoryOut:
    return MemoryOut(id=row.id, category=row.category, key=row.key, value=row.value_json,
                     source_type=row.source_type, version=row.version,
                     created_at=row.created_at, updated_at=row.updated_at)


def is_unique_violation(exc: IntegrityError) -> bool:
    original = exc.orig
    return getattr(original, "sqlstate", None) == "23505" or getattr(original, "sqlite_errorcode", None) in (1555, 2067)


def _integrity(db: Session, exc: IntegrityError) -> None:
    db.rollback()
    # Only a unique violation is a duplicate key; all other DB failures stay generic.
    if is_unique_violation(exc):
        raise MemoryError("MEMORY_KEY_CONFLICT", "A memory with this key already exists", 409) from None
    raise MemoryError("MEMORY_STORAGE_ERROR", "Memory storage is unavailable", 503) from None


def _unavailable(db: Session) -> None:
    db.rollback()
    raise MemoryError("MEMORY_STORAGE_ERROR", "Memory storage is unavailable", 503) from None


def get_memory(db: Session, memory_id: int) -> MemoryOut:
    try:
        row = db.get(PersonalMemory, memory_id)
        if row is None:
            raise MemoryError("MEMORY_NOT_FOUND", "Memory does not exist", 404)
        return memory_out(row)
    except SQLAlchemyError:
        _unavailable(db)


def list_memories(db: Session, category: str | None) -> list[MemoryOut]:
    statement = select(PersonalMemory).order_by(PersonalMemory.category, PersonalMemory.key)
    if category is not None:
        statement = statement.where(PersonalMemory.category == category)
    try:
        return [memory_out(row) for row in db.scalars(statement)]
    except SQLAlchemyError:
        _unavailable(db)


def insert_memory_row(db: Session, *, category: str, key: str, value: Any, source_type: str,
                      now: datetime) -> PersonalMemory:
    """Insert version 1 and flush. Raises IntegrityError on duplicate key; no commit/rollback."""
    row = PersonalMemory(category=category, key=key, value_json=value,
                         source_type=source_type, version=1, created_at=now, updated_at=now)
    db.add(row)
    db.flush()
    return row


def cas_update_memory_row(db: Session, memory_id: int, expected_version: int, *, category: str,
                          key: str, value: Any, source_type: str, now: datetime,
                          bound_category: str | None = None,
                          bound_key: str | None = None) -> PersonalMemory | None:
    """Compare-and-swap in ONE SQL statement; a read-then-write check races.

    Returns None when the ID/version (and optional category/key binding) no longer
    match. Flushes only; the caller owns commit/rollback.
    """
    conditions = [PersonalMemory.id == memory_id, PersonalMemory.version == expected_version]
    if bound_category is not None:
        conditions.append(PersonalMemory.category == bound_category)
    if bound_key is not None:
        conditions.append(PersonalMemory.key == bound_key)
    return db.scalars(update(PersonalMemory).where(*conditions).values(
        category=category, key=key, value_json=value, source_type=source_type,
        version=PersonalMemory.version + 1, updated_at=now,
    ).returning(PersonalMemory), execution_options={"populate_existing": True}).one_or_none()


def create_memory(db: Session, payload: MemoryCreate, clock: Clock) -> MemoryOut:
    try:
        row = insert_memory_row(db, category=payload.category, key=payload.key, value=payload.value,
                                source_type="manual", now=clock.now())
        result = memory_out(row)
        db.commit()
        return result
    except IntegrityError as exc:
        _integrity(db, exc)
    except SQLAlchemyError:
        _unavailable(db)


def update_memory(db: Session, memory_id: int, payload: MemoryUpdate, clock: Clock) -> MemoryOut:
    try:
        # Manual edits always reset provenance to manual.
        row = cas_update_memory_row(db, memory_id, payload.expected_version, category=payload.category,
                                    key=payload.key, value=payload.value, source_type="manual",
                                    now=clock.now())
        if row is None:
            exists = db.scalar(select(PersonalMemory.id).where(PersonalMemory.id == memory_id))
            db.rollback()
            if exists is None:
                raise MemoryError("MEMORY_NOT_FOUND", "Memory does not exist", 404)
            raise MemoryError("MEMORY_VERSION_CONFLICT", "Memory changed since it was loaded. Refresh and try again.", 409)
        result = memory_out(row)
        db.commit()
        return result
    except IntegrityError as exc:
        _integrity(db, exc)
    except SQLAlchemyError:
        _unavailable(db)


def delete_memory(db: Session, memory_id: int) -> None:
    try:
        removed = db.scalar(delete(PersonalMemory).where(PersonalMemory.id == memory_id).returning(PersonalMemory.id))
        if removed is None:
            db.rollback()
            raise MemoryError("MEMORY_NOT_FOUND", "Memory does not exist", 404)
        db.commit()
    except SQLAlchemyError:
        _unavailable(db)
