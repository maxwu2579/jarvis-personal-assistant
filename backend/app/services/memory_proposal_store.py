"""Process-local pending Memory proposals (Phase 10B V1, single API process only).

Pending proposals live in RAM only: no disk, browser storage or distributed
cache. A restart loses them; only committed confirmation receipts survive.
Each entry keeps only what Confirm needs — never the user sentence, the raw
model response or the prompt.

Expiry uses an injectable monotonic clock; UTC timestamps are display metadata.
`lock` is the single synchronization mechanism shared by Confirm, Cancel and
proposal invalidation. It is never held while calling the LLM.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

PROPOSAL_TTL_SECONDS = 600.0
PROPOSAL_CAPACITY = 128
CLEANUP_INTERVAL_SECONDS = 30.0


class ProposalCapacityExceeded(Exception):
    """All slots hold still-valid proposals; valid proposals are never evicted."""


@dataclass(frozen=True)
class PendingMemoryProposal:
    proposal_id: str
    operation: str  # CREATE | UPDATE
    key: str
    category: str
    new_value: object
    old_value: object | None
    target_memory_id: int | None
    expected_version: int | None
    created_at: datetime
    expires_at: datetime
    expires_monotonic: float


class MemoryProposalStore:
    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic,
                 ttl_seconds: float = PROPOSAL_TTL_SECONDS, capacity: int = PROPOSAL_CAPACITY):
        self._monotonic = monotonic
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self._pending: dict[str, PendingMemoryProposal] = {}
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def expiry_deadline(self) -> float:
        return self._monotonic() + self.ttl_seconds

    def purge_expired(self) -> int:
        with self.lock:
            now = self._monotonic()
            expired = [pid for pid, item in self._pending.items() if item.expires_monotonic <= now]
            for pid in expired:
                del self._pending[pid]
            return len(expired)

    def add(self, proposal: PendingMemoryProposal) -> None:
        with self.lock:
            self.purge_expired()
            if len(self._pending) >= self.capacity:
                raise ProposalCapacityExceeded()
            self._pending[proposal.proposal_id] = proposal

    def get_valid(self, proposal_id: str) -> PendingMemoryProposal | None:
        """Checks expiry on access; an expired entry is removed and reported absent."""
        with self.lock:
            item = self._pending.get(proposal_id)
            if item is None:
                return None
            if item.expires_monotonic <= self._monotonic():
                del self._pending[proposal_id]
                return None
            return item

    def discard(self, proposal_id: str) -> None:
        with self.lock:
            self._pending.pop(proposal_id, None)

    def __len__(self) -> int:
        with self.lock:
            return len(self._pending)

    # ---- periodic cleanup (approximately every 30 seconds) ----

    def start_cleanup(self, interval_seconds: float = CLEANUP_INTERVAL_SECONDS) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                self.purge_expired()

        self._thread = threading.Thread(target=loop, name="memory-proposal-cleanup", daemon=True)
        self._thread.start()

    def stop_cleanup(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


_STORE = MemoryProposalStore()


def get_memory_proposal_store() -> MemoryProposalStore:
    """FastAPI dependency: the single process-wide store (tests override it)."""
    return _STORE
