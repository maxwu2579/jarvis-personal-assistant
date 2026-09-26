"""Explicit CRUD only. No integration with LLMs, workers or other domains."""

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


def _out(row: PersonalMemory) -> MemoryOut:
    return MemoryOut(id=row.id, category=row.category, key=row.key, value=row.value_json,
                     source_type=row.source_type, version=row.version,
                     created_at=row.created_at, updated_at=row.updated_at)


def _integrity(db: Session, exc: IntegrityError) -> None:
    db.rollback()
    # Only a unique violation is a duplicate key; all other DB failures stay generic.
    original = exc.orig
    if getattr(original, "sqlstate", None) == "23505" or getattr(original, "sqlite_errorcode", None) in (1555, 2067):
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
        return _out(row)
    except SQLAlchemyError:
        _unavailable(db)


def list_memories(db: Session, category: str | None) -> list[MemoryOut]:
    statement = select(PersonalMemory).order_by(PersonalMemory.category, PersonalMemory.key)
    if category is not None:
        statement = statement.where(PersonalMemory.category == category)
    try:
        return [_out(row) for row in db.scalars(statement)]
    except SQLAlchemyError:
        _unavailable(db)


def create_memory(db: Session, payload: MemoryCreate, clock: Clock) -> MemoryOut:
    now = clock.now()
    row = PersonalMemory(category=payload.category, key=payload.key, value_json=payload.value,
                         source_type="manual", version=1, created_at=now, updated_at=now)
    try:
        db.add(row)
        db.flush()
        result = _out(row)
        db.commit()
        return result
    except IntegrityError as exc:
        _integrity(db, exc)
    except SQLAlchemyError:
        _unavailable(db)


def update_memory(db: Session, memory_id: int, payload: MemoryUpdate, clock: Clock) -> MemoryOut:
    try:
        # Compare-and-swap in ONE SQL statement; a read-then-write check races.
        row = db.scalars(update(PersonalMemory).where(
            PersonalMemory.id == memory_id, PersonalMemory.version == payload.expected_version
        ).values(category=payload.category, key=payload.key, value_json=payload.value,
                 version=PersonalMemory.version + 1, updated_at=clock.now())
          .returning(PersonalMemory), execution_options={"populate_existing": True}).one_or_none()
        if row is None:
            exists = db.scalar(select(PersonalMemory.id).where(PersonalMemory.id == memory_id))
            db.rollback()
            if exists is None:
                raise MemoryError("MEMORY_NOT_FOUND", "Memory does not exist", 404)
            raise MemoryError("MEMORY_VERSION_CONFLICT", "Memory changed since it was loaded. Refresh and try again.", 409)
        result = _out(row)
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
