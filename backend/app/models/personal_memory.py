"""User-managed stable facts; no operational state or historical values."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PersonalMemory(Base):
    __tablename__ = "personal_memories"
    __table_args__ = (
        UniqueConstraint("key", name="uq_personal_memories_key"),
        CheckConstraint("category IN ('preference','stable_fact','constraint')", name="ck_memory_category"),
        CheckConstraint("source_type IN ('manual','user_confirmed')", name="ck_memory_source"),
        CheckConstraint("version >= 1", name="ck_memory_version"),
        Index("ix_personal_memories_category_key", "category", "key"),
        {"sqlite_autoincrement": True},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
