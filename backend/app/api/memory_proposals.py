"""Phase 10B explicit Memory Proposal / Confirmation routes (Memory tab only).

Registered before the /api/memories/{memory_id} routes; every response from
these endpoints — success or error — carries Cache-Control: no-store.
There is no proposal list/history endpoint.
"""

import inspect
from typing import Callable

from fastapi import APIRouter, Depends, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.database import get_db
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.schemas.memory_proposal import (
    MemoryCancelOut,
    MemoryConfirmOut,
    MemoryConfirmRequest,
    MemoryProposalRequest,
    MemoryProposalResponse,
)
from app.services import memory_proposal_service
from app.services.memory_proposal_store import MemoryProposalStore, get_memory_proposal_store


class NoStoreRoute(APIRoute):
    """Applies Cache-Control: no-store to success and error responses alike."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original(request)
            except Exception as exc:  # noqa: BLE001 - delegate to the app's safe handlers
                handler_fn = None
                for cls in type(exc).__mro__:
                    handler_fn = request.app.exception_handlers.get(cls)
                    if handler_fn is not None:
                        break
                if handler_fn is None:
                    raise
                response = handler_fn(request, exc)
                if inspect.isawaitable(response):
                    response = await response
            response.headers["Cache-Control"] = "no-store"
            return response

        return handler


router = APIRouter(prefix="/api/memories/proposals", tags=["memories"], route_class=NoStoreRoute)


@router.post("", response_model=MemoryProposalResponse, response_model_exclude_none=False)
def generate(payload: MemoryProposalRequest, db: Session = Depends(get_db),
             provider: LLMProvider = Depends(get_llm_provider), clock: Clock = Depends(get_clock),
             store: MemoryProposalStore = Depends(get_memory_proposal_store)):
    return memory_proposal_service.generate_proposal(db, provider, store, clock, payload.content)


@router.post("/{proposal_id}/confirm", response_model=MemoryConfirmOut)
def confirm(proposal_id: str, payload: MemoryConfirmRequest, db: Session = Depends(get_db),
            clock: Clock = Depends(get_clock), store: MemoryProposalStore = Depends(get_memory_proposal_store)):
    return memory_proposal_service.confirm_proposal(db, store, clock, proposal_id)


@router.delete("/{proposal_id}", response_model=MemoryConfirmOut | MemoryCancelOut)
def cancel(proposal_id: str, db: Session = Depends(get_db),
           store: MemoryProposalStore = Depends(get_memory_proposal_store)):
    return memory_proposal_service.cancel_proposal(db, store, proposal_id)
