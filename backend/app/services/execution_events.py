"""不可变审计事件服务（Phase 6C）。

append-only 的准确边界（应用层）：
- 服务层只暴露 append_event（INSERT）；本模块**没有**任何 UPDATE / DELETE
  路径——业务代码无法通过服务改写或删除事件；
- UNIQUE(execution_id, sequence_number) 是审计约束：序列不重叠、不覆写
  （重复序列 → IntegrityError → EventConflictError，事务回滚）；
- 不声称数据库管理员也无法修改（无 DB 触发器），"immutable" 指应用层
  append-only + 审计约束；
- 事件只在状态实际变更的事务内写入（调用方负责把 append_event 与状态
  写入放在同一事务并提交；回滚的事件随事务一起消失）；
- sequence_number：同事务内 MAX+1 产生（事务内可见未提交事件，单调），
  并发下由 UNIQUE 约束最终裁决；
- payload 写入前必须经过 sanitize_result_value（敏感字段名 → <redacted>，
  长字符串截断），绝不原样落盘；
- 事件类型白名单：未知类型是编程错误，直接 ValueError（不进入数据库）。

已定义事件类型（23 种）：
EXECUTION_CREATED / EXECUTION_QUEUED / EXECUTION_CLAIMED /
EXECUTION_STARTED / STEP_STARTED / STEP_SUCCEEDED / STEP_FAILED /
CONFIRMATION_REQUIRED / CONFIRMATION_GRANTED / CONFIRMATION_REJECTED /
RETRY_SCHEDULED / CANCEL_REQUESTED / EXECUTION_SUCCEEDED /
EXECUTION_FAILED / EXECUTION_CANCELLED / EXECUTION_TIMED_OUT /
LEASE_RECOVERED / EXECUTION_SCHEDULED / SCHEDULE_DUE /
MISFIRE_DETECTED / MISFIRE_RUN_IMMEDIATELY / MISFIRE_SKIPPED /
MISFIRE_FAILED
（6D 后六种为一次性调度审计：EXECUTION_SCHEDULED 在带 run_at 创建时同事务
写入；SCHEDULE_DUE 在到期 claim 裁决成功时写入；MISFIRE_* 在 misfire
检测/策略裁决成功后写入——事件绝不悬空、不做状态变化也绝不写。）

不要求为旧历史数据补造事件（6A/6B 时代的事件不存在，属正常）。
"""

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.database import DIALECT
from app.core.dialect import coerce_utc_for_db
from app.core.sanitize import sanitize_result_value
from app.models.execution_event import ExecutionEvent
from app.services.execution_errors import EventConflictError

# 合法事件类型白名单（服务层校验；未知类型是编程错误）
APPEND_ONLY_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "EXECUTION_CREATED",
        "EXECUTION_QUEUED",
        "EXECUTION_CLAIMED",
        "EXECUTION_STARTED",
        "STEP_STARTED",
        "STEP_SUCCEEDED",
        "STEP_FAILED",
        "CONFIRMATION_REQUIRED",
        "CONFIRMATION_GRANTED",
        "CONFIRMATION_REJECTED",
        "RETRY_SCHEDULED",
        "CANCEL_REQUESTED",
        "EXECUTION_SUCCEEDED",
        "EXECUTION_FAILED",
        "EXECUTION_CANCELLED",
        "EXECUTION_TIMED_OUT",
        "LEASE_RECOVERED",
        "EXECUTION_SCHEDULED",
        "SCHEDULE_DUE",
        "MISFIRE_DETECTED",
        "MISFIRE_RUN_IMMEDIATELY",
        "MISFIRE_SKIPPED",
        "MISFIRE_FAILED",
    }
)


def _to_db_utc(value: datetime) -> datetime:
    """写入数据库前的 UTC 归一化（方言唯一时区接缝，见 core/dialect.py）。"""
    return coerce_utc_for_db(value, DIALECT)


def _next_sequence(db: Session, execution_id: int) -> int:
    """同事务内 MAX(sequence_number) + 1（事务内可见未提交事件，单调递增）。

    并发安全由调用方事务 + UNIQUE 约束兜底：冲突 → IntegrityError →
    EventConflictError（append_event 内捕获）。
    """
    current = db.execute(
        select(func.coalesce(func.max(ExecutionEvent.sequence_number), 0)).where(
            ExecutionEvent.execution_id == execution_id
        )
    ).scalar_one()
    return int(current) + 1


def append_event(
    db: Session,
    *,
    execution_id: int,
    event_type: str,
    attempt_id: int | None = None,
    step_id: int | None = None,
    actor_type: str = "service",
    actor_id: str = "execution_service",
    correlation_id: str | None = None,
    payload: dict | None = None,
    clock: Clock | None = None,
) -> ExecutionEvent:
    """追加一条审计事件（INSERT only；本模块无 UPDATE/DELETE 路径）。

    不提交事务——调用方把本事件与状态变更放在同一事务内提交，保证
    事件与状态原子。payload 写入前经 sanitize_result_value 脱敏。
    """
    if event_type not in APPEND_ONLY_EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}")
    now = _to_db_utc((clock or SystemClock()).now())
    payload_text: str | None = None
    if payload is not None:
        payload_text = json.dumps(
            sanitize_result_value(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    event = ExecutionEvent(
        execution_id=execution_id,
        attempt_id=attempt_id,
        step_id=step_id,
        event_type=event_type,
        sequence_number=_next_sequence(db, execution_id),
        actor_type=actor_type,
        actor_id=actor_id,
        correlation_id=correlation_id,
        payload=payload_text,
        created_at=now,
    )
    db.add(event)
    try:
        db.flush()  # IntegrityError（序列冲突）在此抛出
    except IntegrityError as exc:
        db.rollback()
        raise EventConflictError(execution_id, event_type) from exc
    return event


def list_events(
    db: Session,
    execution_id: int,
    *,
    limit: int | None = None,
    offset: int | None = None,
) -> list[ExecutionEvent]:
    """按 sequence_number 升序取某 execution 的事件（审计查询，6E 分页）。

    offset 为 6E API 分页参数（向后兼容：缺省与旧行为一致）。
    """
    query = (
        select(ExecutionEvent)
        .where(ExecutionEvent.execution_id == execution_id)
        .order_by(ExecutionEvent.sequence_number)
    )
    if offset is not None:
        query = query.offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return list(db.execute(query).scalars().all())
