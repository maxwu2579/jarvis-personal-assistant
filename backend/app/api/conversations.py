"""对话 API 路由。"""

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.database import get_db
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.schemas.chat import (
    ChatExchangeOut,
    ConversationCreate,
    ConversationOut,
    MessageCreate,
    MessageOut,
    UsageOut,
)
from app.schemas.action_proposal import (
    ActionProposalExchangeOut,
    ActionProposalConfirmOut,
    ActionProposalConfirmRequest,
    ActionProposalOut,
    ActionProposalRequest,
)
from app.schemas.reminder import ReminderOut
from app.schemas.execution import ExecutionOut
from app.schemas.proposal import (
    TaskProposalExchangeOut,
    TaskProposalOut,
    TaskProposalRequest,
    TaskProposalWithTaskOut,
)
from app.schemas.task import TaskOut
from app.services import (
    action_confirm_service,
    action_proposal_service,
    chat_service,
    proposal_service,
)

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


@router.post("", response_model=ConversationOut, status_code=201, summary="创建对话")
def create_conversation(payload: ConversationCreate, db: Session = Depends(get_db)):
    return chat_service.create_conversation(db, title=payload.title)


@router.get("", response_model=list[ConversationOut], summary="对话列表（按最近活动排序）")
def list_conversations(db: Session = Depends(get_db)):
    return chat_service.list_conversations(db)


@router.get("/{conversation_id}", response_model=ConversationOut, summary="获取单个对话")
def get_conversation(conversation_id: int, db: Session = Depends(get_db)):
    return chat_service.get_conversation(db, conversation_id)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut], summary="对话消息列表（时间升序）")
def list_messages(conversation_id: int, db: Session = Depends(get_db)):
    return chat_service.list_messages(db, conversation_id)


@router.post("/{conversation_id}/messages", response_model=ChatExchangeOut, status_code=201, summary="发送消息并调用 LLM")
def send_message(
    conversation_id: int,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
):
    user_message, assistant_message, response = chat_service.send_message(
        db, conversation_id, payload.content, provider
    )
    return ChatExchangeOut(
        user_message=MessageOut.model_validate(user_message),
        assistant_message=MessageOut.model_validate(assistant_message),
        usage=UsageOut(
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            latency_ms=response.latency_ms,
        ),
    )


@router.post(
    "/{conversation_id}/action-proposals",
    response_model=ActionProposalExchangeOut,
    response_model_exclude_unset=True,
    summary="生成只读 Action Proposal（Phase 8A，不执行、不写业务数据）",
)
def create_action_proposal(
    conversation_id: int,
    payload: ActionProposalRequest,
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
    clock: Clock = Depends(get_clock),
):
    proposal, response = action_proposal_service.generate_action_proposal(
        db,
        conversation_id=conversation_id,
        content=payload.content,
        timezone=payload.timezone,
        provider=provider,
        clock=clock,
    )
    prompt_tokens, completion_tokens, latency_ms = action_proposal_service.usage_values(response)
    return ActionProposalExchangeOut(
        proposal=ActionProposalOut.model_validate(proposal),
        usage=UsageOut(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
        ),
    )


@router.post(
    "/{conversation_id}/action-proposals/confirm",
    response_model=ActionProposalConfirmOut,
    response_model_exclude_unset=True,
    summary="显式确认 Action Proposal（Phase 8B，原子且幂等）",
)
def confirm_action_proposal(
    conversation_id: int,
    payload: ActionProposalConfirmRequest,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    result = action_confirm_service.confirm_action_proposal(
        db,
        conversation_id=conversation_id,
        proposal=payload.proposal,
        idempotency_key=idempotency_key,
        correlation_id=getattr(request.state, "correlation_id", None),
        clock=clock,
    )
    response.status_code = 200 if result.replayed else 201
    output = ActionProposalConfirmOut(
        confirmation_id=result.confirmation_id,
        action=result.action,
        task=TaskOut.model_validate(result.task) if result.task is not None else None,
        reminder=(
            ReminderOut.model_validate(result.reminder)
            if result.reminder is not None
            else None
        ),
        replayed=result.replayed,
    )
    if result.execution is not None:
        output.execution = ExecutionOut.model_validate(result.execution)
    return output


@router.post(
    "/{conversation_id}/task-proposals",
    response_model=TaskProposalExchangeOut,
    status_code=201,
    summary="从自然语言生成任务建议（仅创建 DRAFT，确认需调用现有 confirm 接口）",
)
def create_task_proposal(
    conversation_id: int,
    payload: TaskProposalRequest,
    db: Session = Depends(get_db),
    provider: LLMProvider = Depends(get_llm_provider),
    clock: Clock = Depends(get_clock),
):
    proposal, task, user_message, assistant_message, proposal_record = proposal_service.create_task_proposal(
        db,
        conversation_id=conversation_id,
        content=payload.content,
        timezone=payload.timezone,
        provider=provider,
        clock=clock,
    )
    return TaskProposalExchangeOut(
        user_message=MessageOut.model_validate(user_message),
        assistant_message=MessageOut.model_validate(assistant_message),
        proposal=TaskProposalOut.model_validate(proposal_record),
        created_task=TaskOut.model_validate(task),
        usage=UsageOut(
            prompt_tokens=assistant_message.prompt_tokens,
            completion_tokens=assistant_message.completion_tokens,
            latency_ms=assistant_message.latency_ms,
        ),
    )


@router.get(
    "/{conversation_id}/task-proposals",
    response_model=list[TaskProposalWithTaskOut],
    summary="对话的任务建议列表（刷新页面后重新加载）",
)
def list_task_proposals(conversation_id: int, db: Session = Depends(get_db)):
    records = proposal_service.list_task_proposals(db, conversation_id)
    result = []
    for record in records:
        task = proposal_service.get_task_proposal_task(db, record)
        result.append(
            TaskProposalWithTaskOut(
                proposal=TaskProposalOut.model_validate(record),
                task=TaskOut.model_validate(task) if task is not None else None,
            )
        )
    return result
