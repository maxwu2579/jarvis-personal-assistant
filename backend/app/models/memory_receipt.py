"""Minimal Phase 10B confirmation receipts: idempotency facts, never Memory content.

No key, category, value, input sentence, prompt or model output is stored here.
memory_id deliberately has no foreign key so a receipt survives Memory deletion.
"""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MemoryConfirmationReceipt(Base):
    __tablename__ = "memory_confirmation_receipts"
    __table_args__ = (
        CheckConstraint("length(proposal_id) = 36", name="ck_memory_receipt_proposal_id"),
        CheckConstraint("operation IN ('CREATE','UPDATE')", name="ck_memory_receipt_operation"),
        CheckConstraint("memory_id >= 1", name="ck_memory_receipt_memory_id"),
        CheckConstraint("applied_version >= 1", name="ck_memory_receipt_version"),
        CheckConstraint("status = 'APPLIED'", name="ck_memory_receipt_status"),
    )

    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation: Mapped[str] = mapped_column(String(10), nullable=False)
    memory_id: Mapped[int] = mapped_column(Integer, nullable=False)
    applied_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
