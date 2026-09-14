"""任务建议编排：自然语言 → 结构化建议 → 验证 → DRAFT 任务（原子落库）。

安全闭环：
1. 模型只输出数据建议（Structured Output），不接触数据库、不调用任何服务；
2. 后端用 TaskProposal schema + 复用的 TaskCreate 规则二次验证；
3. 只有 action=create_task_draft 白名单动作会被执行；
4. 创建结果永远是 DRAFT；确认只能由用户调用现有 confirm 接口完成；
5. 不自动重试（生成不幂等，重试可能重复创建任务）。

失败状态（明确且可审计）：
- Conversation 不存在：什么都不写；
- Provider 失败 / Structured Output 校验失败：USER 消息保留，不创建任务、
  不保存 ASSISTANT、不保存 proposal；
- 成功路径：USER + ASSISTANT + Task + TaskProposal 在**同一事务**提交，
  任何一个写入失败整体回滚，不会出现“任务已创建但记录缺失”的中间态。
"""

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.llm.base import LLMProvider
from app.llm.messages import build_provider_messages
from app.llm.prompts import build_proposal_system_prompt, build_proposal_user_prompt
from app.models.conversation import Conversation, Message, MessageRole
from app.models.proposal import TaskProposal, TaskProposalStatus
from app.models.task import Task, utcnow
from app.schemas.proposal import TaskProposal as TaskProposalSchema
from app.services import task_service
from app.services.chat_service import get_conversation

logger = logging.getLogger(__name__)


class ProposalSchemaError(Exception):
    """模型输出无法构成合法 Proposal（非 JSON / 缺失字段 / 非法 action 等）。

    映射为 502 + LLM_SCHEMA_ERROR。不携带模型原始输出。
    携带审计字段：error_code（稳定错误码）、error_detail（可审计简述，不含敏感数据）、
    action（可解析出的模型提议动作，解析失败为 None）。
    """

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        error_detail: str,
        action: str | None = None,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.error_detail = error_detail
        self.action = action


def _validation_summary(exc: Exception) -> tuple[str, str]:
    """从 Pydantic ValidationError 提取可审计摘要并分类。

    分类规则：
    - 错误全部来自 arguments 内部的参数规则（value_error）→ TASK_VALIDATION_FAILED
      （空白 title、无时区 due_at 等 TaskCreate 规则）；
    - 其余（顶层/结构缺失、类型错误、extra 字段、非法 action 等）→ SCHEMA_INVALID。
    摘要只含字段与规则文本，不含输入值。
    """
    errors = getattr(exc, "errors", lambda: [])()
    parts = []
    for error in errors[:3]:
        loc = ".".join(str(part) for part in error.get("loc", []))
        msg = error.get("msg", "")
        parts.append(f"{loc}: {msg}" if loc else msg)
    summary = "; ".join(parts) if parts else "validation failed"

    arg_rule_errors = [
        e
        for e in errors
        if e.get("type") == "value_error"
        and bool(e.get("loc"))
        and e["loc"][0] == "arguments"
    ]
    if errors and len(arg_rule_errors) == len(errors):
        return "TASK_VALIDATION_FAILED", summary
    return "SCHEMA_INVALID", summary


def _parse_and_validate(raw_text: str) -> TaskProposalSchema:
    """解析模型输出并做 Pydantic 验证。任何失败抛 ProposalSchemaError（带审计字段）。"""
    try:
        payload = json.loads(raw_text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ProposalSchemaError(
            "model output is not valid JSON",
            error_code="SCHEMA_INVALID",
            error_detail="model output is not valid JSON",
        ) from exc
    if not isinstance(payload, dict):
        raise ProposalSchemaError(
            "model output must be a JSON object",
            error_code="SCHEMA_INVALID",
            error_detail="model output is not a JSON object",
        )
    action = payload.get("action")
    action_text = action if isinstance(action, str) and action else None
    try:
        return TaskProposalSchema.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 校验失败统一收敛
        error_code, error_detail = _validation_summary(exc)
        raise ProposalSchemaError(
            "model output does not match the proposal schema",
            error_code=error_code,
            error_detail=error_detail,
            action=action_text,
        ) from exc


def _record_rejected(
    db: Session,
    *,
    conversation_id: int,
    user_message_id: int,
    action: str | None,
    error_code: str,
    error_detail: str,
) -> None:
    """保存 REJECTED 审计记录：不创建任务、不创建伪造 ASSISTANT、不保存原始输出。"""
    record = TaskProposal(
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=None,
        task_id=None,
        action=action,
        arguments_json=None,
        explanation=None,
        status=TaskProposalStatus.REJECTED.value,
        error_code=error_code,
        error_detail=error_detail,
    )
    db.add(record)
    db.commit()


def create_task_proposal(
    db: Session,
    *,
    conversation_id: int,
    content: str,
    timezone: str,
    provider: LLMProvider,
    clock: Clock,
) -> tuple[TaskProposalSchema, object, Message, Message]:
    """编排：见模块 docstring 的失败状态约定。返回 (proposal, task, user_message, assistant_message)。"""
    get_conversation(db, conversation_id)  # 不存在 → 404，什么都不写

    # 1. USER 消息先落库并提交
    user_message = Message(
        conversation_id=conversation_id, role=MessageRole.USER.value, content=content
    )
    db.add(user_message)
    db.commit()
    db.refresh(user_message)

    # 2. 构建 Prompt：当前日期（用户时区）与时区由后端注入
    tz = ZoneInfo(timezone)
    now_utc = clock.now()
    current_date = now_utc.astimezone(tz).strftime("%Y-%m-%d")
    system_prompt = build_proposal_system_prompt(
        current_date=current_date, user_timezone=timezone
    )
    user_prompt = build_proposal_user_prompt(content)

    history = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .all()
    )
    provider_messages = build_provider_messages(history, system_prompt=system_prompt)
    provider_messages.append({"role": "user", "content": user_prompt})

    # 3. 调用模型（Structured Output）。失败只保留 USER 消息。
    response = provider.generate(provider_messages, response_schema=TaskProposalSchema)

    # 4. 二次验证（即使供应商声称符合 Schema 也要验证）。
    #    失败：保存 REJECTED 审计记录（不创建任务、不伪造 ASSISTANT），然后抛 502。
    try:
        proposal = _parse_and_validate(response.text)
    except ProposalSchemaError as exc:
        logger.warning(
            "proposal.rejected error_code=%s detail=%s",
            exc.error_code,
            exc.error_detail,
        )
        _record_rejected(
            db,
            conversation_id=conversation_id,
            user_message_id=user_message.id,
            action=exc.action,
            error_code=exc.error_code,
            error_detail=exc.error_detail,
        )
        raise

    # 5. 同一事务：Task + ASSISTANT + Proposal 一起提交
    task = task_service.create_task(
        db,
        title=proposal.arguments.title,
        description=proposal.arguments.description,
        due_at=proposal.arguments.due_at,
        commit=False,
    )
    assistant_message = Message(
        conversation_id=conversation_id,
        role=MessageRole.ASSISTANT.value,
        content=proposal.explanation,
        model=response.model,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        latency_ms=response.latency_ms,
    )
    db.add(assistant_message)
    db.flush()  # 拿到 assistant_message.id

    proposal_record = TaskProposal(
        conversation_id=conversation_id,
        user_message_id=user_message.id,
        assistant_message_id=assistant_message.id,
        task_id=task.id,
        action=proposal.action,
        arguments_json=json.dumps(proposal.arguments.model_dump(mode="json"), ensure_ascii=False),
        explanation=proposal.explanation,
        status=TaskProposalStatus.SUCCEEDED.value,
    )
    db.add(proposal_record)

    conversation = db.get(Conversation, conversation_id)
    if conversation is not None:
        conversation.updated_at = utcnow()

    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(task)
    db.refresh(assistant_message)
    db.refresh(proposal_record)

    logger.info(
        "proposal.completed",
        extra={
            "conversation_id": conversation_id,
            "user_message_id": user_message.id,
            "assistant_message_id": assistant_message.id,
            "task_id": task.id,
            "proposal_id": proposal_record.id,
            "action": proposal.action,
            "model": response.model,
            "latency_ms": response.latency_ms,
        },
    )
    return proposal, task, user_message, assistant_message, proposal_record


def list_task_proposals(db: Session, conversation_id: int) -> list[TaskProposal]:
    get_conversation(db, conversation_id)  # 不存在 → 404
    return (
        db.query(TaskProposal)
        .filter(TaskProposal.conversation_id == conversation_id)
        .order_by(TaskProposal.created_at.asc(), TaskProposal.id.asc())
        .all()
    )


def get_task_proposal_task(db: Session, proposal: TaskProposal) -> Task | None:
    """读取 proposal 对应的任务（刷新页面时展示状态）；REJECTED 记录无任务。"""
    if proposal.task_id is None:
        return None
    return db.get(Task, proposal.task_id)
