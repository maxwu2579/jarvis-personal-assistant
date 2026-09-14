"""Execution HTTP API（Phase 6E + Phase 7.2A/7.2A2）。

八条路由全部是既有 domain/service 的薄封装：
- create → execution_service.create_execution（幂等裁决与 replayed 标记
  同源于 service，本层绝不重新实现幂等逻辑）；
- cancel → request_cancel（CREATED/QUEUED→CANCELLED、CLAIMED/RUNNING→
  CANCEL_REQUESTED、重复取消幂等、终态稳定 409）；
- retry → retry_execution（FAILED→QUEUED CAS，双击幂等，无内存缓存）；
- confirm/reject → execution_steps.grant/reject_confirmation（6C 交付的
  confirmation service：显式 actor 必填、绑定 hash、事件同事务）；
- 列表：limit/offset 分页 + status/execution_type/created 范围白名单过滤
  + sort 白名单 + id 稳定 tie-break（reminders 分页约定）；
- events：sequence 升序 + 有限分页，payload 已脱敏。
- enqueue：仅复用 execution_service.enqueue 执行 CREATED → QUEUED，
  不直接运行工具；
- steps：只读返回 execution_steps 当前行，按 step_index 稳定排序。

安全边界：响应不含 payload 正文、lease_token/lease_owner/lease_expires_at；
body extra="forbid"（idempotency_key/confirmation_status 等旁路字段 422）；
错误经 ExecutionError handler 输出统一信封（code/message/details/
correlation_id）。
"""

import json
import logging
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import AfterValidator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.database import DIALECT, get_db
from app.core.dialect import coerce_utc_for_db
from app.models.execution import Execution, ExecutionStatus
from app.models.execution_step import ConfirmationStatus, ExecutionStep
from app.schemas.execution import (
    ConfirmStepRequest,
    ExecutionCreate,
    ExecutionCreateOut,
    ExecutionDetailOut,
    ExecutionDocumentTarget,
    ExecutionCalendarTarget,
    ExecutionCalendarResult,
    ExecutionEventOut,
    ExecutionOut,
    ExecutionStepOut,
)
from app.services.execution_errors import ExecutionNotFoundError, StepInvalidError
from app.services.execution_events import list_events
from app.services.execution_registry import PUBLIC_EXECUTION_TYPES, get_type_spec
from app.services.execution_service import (
    DEFAULT_OWNER_ID,
    create_execution,
    enqueue,
    get_execution,
    request_cancel,
    retry_execution,
)
from app.services.execution_steps import (
    grant_confirmation,
    list_execution_steps,
    reject_confirmation,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/executions", tags=["executions"])

# 分页约定（与 reminders 一致）：默认 50、硬上限 200
MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 50

# 排序字段白名单：不接受任意 SQL 字段/表达式
_SORT_COLUMNS: dict[str, object] = {
    "id": Execution.id,
    "created_at": Execution.created_at,
    "run_at": Execution.run_at,
}


def _require_aware(value: datetime | None) -> datetime | None:
    """时间范围过滤的 UTC 规范：naive → 422（与 6D 拒绝 naive 的语义一致）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError(
            "datetime must include a timezone offset, e.g. "
            "2026-08-11T04:00:00+00:00 (naive datetime is not accepted)"
        )
    return value.astimezone(timezone.utc)


UtcDateTime = Annotated[datetime | None, AfterValidator(_require_aware)]


def _cid(request: Request) -> str | None:
    """correlation ID（CorrelationIdMiddleware 注入 request.state）。"""
    return getattr(request.state, "correlation_id", None)


def _get_public_execution(db: Session, execution_id: int) -> Execution:
    """Resolve only public Execution resources; internal receipts look absent.

    The owner filter alone is not the security boundary: a public endpoint must
    also require membership in the public execution registry.  This keeps an
    internal receipt isolated even if a malformed row ever carries a public
    owner or a non-terminal status.
    """
    execution = get_execution(db, execution_id)
    if (
        execution.owner_id != DEFAULT_OWNER_ID
        or execution.execution_type not in PUBLIC_EXECUTION_TYPES
    ):
        raise ExecutionNotFoundError(execution_id)
    return execution


def _resolve_step(db: Session, execution_id: int, step_id: int) -> ExecutionStep:
    """解析 step_id 并校验归属（规格九：不允许确认其他 execution 的 step）。"""
    _get_public_execution(db, execution_id)  # internal/不存在 → 404 EXEC_NOT_FOUND
    step = db.get(ExecutionStep, step_id)
    if step is None or step.execution_id != execution_id:
        raise StepInvalidError(
            f"step {step_id} does not belong to execution {execution_id}"
        )
    return step


def _confirm_shortcut(
    step: ExecutionStep, target: ConfirmationStatus, actor_id: str
) -> bool:
    """确认幂等短路（纯读）：同状态且同 actor 的重复请求不重复写事件。

    状态竞态裁决仍在 service 的条件写入，本层不做任何状态变更。
    """
    return (
        step.confirmation_status == target.value
        and step.confirmation_actor_id == actor_id
    )


def _project_execution_target(
    execution: Execution,
) -> ExecutionDocumentTarget | ExecutionCalendarTarget | None:
    """从已存储 payload 显式投影安全业务目标；未知/损坏数据 fail closed。

    仅 documents.reindex 可以公开 document_id。这里不遍历 payload、不做
    动态字段投影，也不把解析错误或内部原文带入响应。其他 execution type
    恒返回 None。
    """
    if execution.execution_type not in {"documents.reindex", "calendar.create_event"}:
        return None
    try:
        payload = json.loads(execution.payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if execution.execution_type == "documents.reindex":
        document_id = payload.get("document_id")
        if type(document_id) is not int:
            return None
        return ExecutionDocumentTarget(document_id=document_id)
    try:
        return ExecutionCalendarTarget(
            title=payload["title"], start_at=payload["start_at"],
            end_at=payload["end_at"], location=payload.get("location"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _project_execution_result(db: Session, execution: Execution) -> ExecutionCalendarResult | None:
    if execution.execution_type != "calendar.create_event" or execution.status != "SUCCEEDED":
        return None
    step = db.execute(
        select(ExecutionStep).where(
            ExecutionStep.execution_id == execution.id,
            ExecutionStep.step_key == "create_event",
        )
    ).scalar_one_or_none()
    try:
        payload = json.loads(step.output_json) if step and step.output_json else None
        return ExecutionCalendarResult.model_validate(payload)
    except (TypeError, ValueError):
        return None


@router.post("", response_model=ExecutionCreateOut)
def create_execution_api(
    body: ExecutionCreate,
    request: Request,
    response: Response,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    """创建 Execution。Idempotency-Key Header 必填（8-128，[A-Za-z0-9._-]）。

    首次 → 201 + replayed=false；同 key 同请求 → 200 + replayed=true；
    同 key 异请求 → 409 EXEC_IDEMPOTENCY_CONFLICT。Header 缺失 → 422。
    """
    if not get_type_spec(body.execution_type).api_creatable:
        from app.services.execution_errors import ExecutionTypeUnknownError
        raise ExecutionTypeUnknownError(body.execution_type)
    execution = create_execution(
        db,
        execution_type=body.execution_type,
        payload=body.payload,
        idempotency_key=idempotency_key,
        run_at=body.run_at,
        correlation_id=_cid(request),
        clock=clock,
    )
    replayed = bool(getattr(execution, "_replayed", False))
    response.status_code = 200 if replayed else 201
    logger.info(
        "execution.api action=create execution_id=%s replayed=%s "
        "correlation_id=%s",
        execution.id, replayed, _cid(request),
    )
    return ExecutionCreateOut(execution=execution, replayed=replayed)


@router.get("", response_model=list[ExecutionOut])
def list_executions_api(
    db: Session = Depends(get_db),
    limit: int | None = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int | None = Query(0, ge=0),
    status: ExecutionStatus | None = Query(None),
    execution_type: str | None = Query(None, max_length=50),
    created_from: UtcDateTime = Query(None),
    created_to: UtcDateTime = Query(None),
    sort: Literal["id", "created_at", "run_at"] = Query("id"),
    order: Literal["asc", "desc"] = Query("asc"),
):
    """列表：白名单过滤 + 稳定排序（排序键白名单 + id tie-break）。"""
    query = select(Execution).where(
        Execution.owner_id == DEFAULT_OWNER_ID,
        Execution.execution_type.in_(tuple(PUBLIC_EXECUTION_TYPES)),
    )
    if status is not None:
        query = query.where(Execution.status == status.value)
    if execution_type is not None:
        query = query.where(Execution.execution_type == execution_type)
    if created_from is not None:
        query = query.where(
            Execution.created_at >= coerce_utc_for_db(created_from, DIALECT)
        )
    if created_to is not None:
        query = query.where(
            Execution.created_at <= coerce_utc_for_db(created_to, DIALECT)
        )
    column = _SORT_COLUMNS[sort]
    ordering = column.asc() if order == "asc" else column.desc()
    query = query.order_by(ordering, Execution.id.asc())  # 稳定 tie-break
    query = query.offset(offset).limit(limit)
    return list(db.execute(query).scalars().all())


@router.get("/{execution_id}", response_model=ExecutionDetailOut)
def get_execution_api(execution_id: int, db: Session = Depends(get_db)):
    """单项：安全公开字段 + allowlist target；纯读且不返回 payload 原文。"""
    execution = _get_public_execution(db, execution_id)
    detail = ExecutionDetailOut.model_validate(execution)
    return detail.model_copy(update={
        "target": _project_execution_target(execution),
        "result": _project_execution_result(db, execution),
    })


@router.post("/{execution_id}/enqueue", response_model=ExecutionOut)
def enqueue_execution_api(
    execution_id: int,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    """显式入队：严格复用既有 CREATED → QUEUED 状态转换。

    非 CREATED 状态沿用现有 EXEC_INVALID_STATE_TRANSITION 安全错误；
    本端点不 claim、不物化步骤，也不直接执行任何工具。
    """
    _get_public_execution(db, execution_id)
    execution = enqueue(db, execution_id, clock=clock)
    logger.info(
        "execution.api action=enqueue execution_id=%s status=%s correlation_id=%s",
        execution_id, execution.status, execution.correlation_id,
    )
    return execution


@router.get("/{execution_id}/steps", response_model=list[ExecutionStepOut])
def list_execution_steps_api(
    execution_id: int,
    db: Session = Depends(get_db),
):
    """只读步骤列表：当前数据库状态，step_index + id 稳定排序。

    响应使用明确的 ExecutionStepOut 安全 schema，不包含 input/output
    原文、attempt/lease 信息或其他 worker 内部字段。
    """
    _get_public_execution(db, execution_id)
    return list_execution_steps(db, execution_id)


@router.get("/{execution_id}/events", response_model=list[ExecutionEventOut])
def list_execution_events_api(
    execution_id: int,
    db: Session = Depends(get_db),
    limit: int | None = Query(DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    offset: int | None = Query(0, ge=0),
):
    """审计事件：sequence_number 升序 + 有限分页；payload 已脱敏。"""
    _get_public_execution(db, execution_id)  # internal/不存在 → 404
    return list_events(db, execution_id, limit=limit, offset=offset)


@router.post("/{execution_id}/cancel", response_model=ExecutionOut)
def cancel_execution_api(
    execution_id: int,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    """取消：未开始 → CANCELLED；执行中 → CANCEL_REQUESTED；重复幂等；
    终态 → 409 EXEC_INVALID_STATE_TRANSITION（状态机裁决，本层不直接写库）。"""
    _get_public_execution(db, execution_id)
    execution = request_cancel(db, execution_id, clock=clock)
    logger.info(
        "execution.api action=cancel execution_id=%s status=%s correlation_id=%s",
        execution_id, execution.status, execution.correlation_id,
    )
    return execution


@router.post("/{execution_id}/retry", response_model=ExecutionOut)
def retry_execution_api(
    execution_id: int,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    """重试 FAILED execution → QUEUED（CAS 幂等：双击不产生重复调度）。
    非 FAILED → 409；成功重试后由 Worker/claim 创建新 attempt。"""
    _get_public_execution(db, execution_id)
    execution = retry_execution(db, execution_id, clock=clock)
    logger.info(
        "execution.api action=retry execution_id=%s status=%s correlation_id=%s",
        execution_id, execution.status, execution.correlation_id,
    )
    return execution


@router.post("/{execution_id}/steps/{step_id}/confirm", response_model=ExecutionStepOut)
def confirm_step_api(
    execution_id: int,
    step_id: int,
    body: ConfirmStepRequest,
    request: Request,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    """确认步骤（显式 actor 必填；body 无法指定 confirmation_status）。
    绑定 execution_id/step_id/tool_name/input_hash；hash 变化后旧确认
    EXPIRED 须重新确认；REJECTED 不可翻转 → 409。"""
    step = _resolve_step(db, execution_id, step_id)
    if _confirm_shortcut(step, ConfirmationStatus.GRANTED, body.actor):
        return step  # 同 actor 重复确认：幂等
    step = grant_confirmation(
        db, execution_id, step.step_key,
        actor_type=body.actor_type, actor_id=body.actor, clock=clock,
    )
    logger.info(
        "execution.api action=confirm execution_id=%s step=%s actor=%s "
        "correlation_id=%s",
        execution_id, step.step_key, body.actor, _cid(request),
    )
    return step


@router.post("/{execution_id}/steps/{step_id}/reject", response_model=ExecutionStepOut)
def reject_step_api(
    execution_id: int,
    step_id: int,
    body: ConfirmStepRequest,
    request: Request,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    """拒绝步骤：稳定失败（EXEC_CONFIRMATION_REJECTED，不重试）；
    GRANTED 已确认不可翻转 → 409；重复拒绝幂等。"""
    step = _resolve_step(db, execution_id, step_id)
    if _confirm_shortcut(step, ConfirmationStatus.REJECTED, body.actor):
        return step  # 同 actor 重复拒绝：幂等
    step = reject_confirmation(
        db, execution_id, step.step_key,
        actor_type=body.actor_type, actor_id=body.actor, clock=clock,
    )
    logger.info(
        "execution.api action=reject execution_id=%s step=%s actor=%s "
        "correlation_id=%s",
        execution_id, step.step_key, body.actor, _cid(request),
    )
    return step
