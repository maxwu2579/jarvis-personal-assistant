"""Manual stable preferences/facts only; operational state belongs to its domain.

Do not store passwords, API keys or authentication codes. Values are inert data.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.database import get_db
from app.schemas.memory import MemoryCategory, MemoryCreate, MemoryOut, MemoryUpdate
from app.services import memory_service

router = APIRouter(prefix="/api/memories", tags=["memories"])


@router.post("", response_model=MemoryOut, status_code=201)
def create(payload: MemoryCreate, db: Session = Depends(get_db), clock: Clock = Depends(get_clock)):
    return memory_service.create_memory(db, payload, clock)


@router.get("", response_model=list[MemoryOut])
def listing(category: MemoryCategory | None = None, db: Session = Depends(get_db)):
    return memory_service.list_memories(db, category)


@router.get("/{memory_id}", response_model=MemoryOut)
def detail(memory_id: int, db: Session = Depends(get_db)):
    return memory_service.get_memory(db, memory_id)


@router.put("/{memory_id}", response_model=MemoryOut)
def replace(memory_id: int, payload: MemoryUpdate, db: Session = Depends(get_db), clock: Clock = Depends(get_clock)):
    return memory_service.update_memory(db, memory_id, payload, clock)


@router.delete("/{memory_id}")
def remove(memory_id: int, db: Session = Depends(get_db)):
    memory_service.delete_memory(db, memory_id)
    return {"deleted": True}
