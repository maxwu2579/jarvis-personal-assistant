"""Execution 领域逻辑（Phase 6A：Execution Domain Foundation）。

职责：执行任务的创建（幂等）与状态机。不感知 HTTP——业务失败抛出领域
异常（services/execution_errors.py），由 6E 的 API 层映射为稳定 HTTP
状态码与错误码。

本阶段边界（严格）：
- 幂等：快速路径先查 + 唯一约束 IntegrityError 并发裁决，绝不只靠先查后写；
- 状态机：全 9 态白名单 + 终态不可逆 + 条件 UPDATE（rowcount 判定），
  非法转换抛 EXEC_INVALID_STATE_TRANSITION；
- 不实现 claim / lease / retry / backoff / timeout 执行逻辑（属 6B）；
- 不提供 HTTP API（属 6E）；run_at 只存储不调度（属 6D）。
"""

import json
import re
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.database import DIALECT
from app.core.dialect import coerce_utc_for_db
from app.models.execution import (
    TERMINAL_STATUSES,
    Execution,
    ExecutionStatus,
)
from app.services.execution_errors import (
    IdempotencyConflictError,
    InvalidExecutionTransitionError,
    InvalidIdempotencyKeyError,
    ExecutionNotFoundError,
    ExecutionRunAtInvalidError,
    ExecutionRunAtNaiveError,
)
from app.services.execution_events import append_event
from app.services.execution_registry import get_type_spec, normalize_payload

# 幂等键规范：8-128 字符，[A-Za-z0-9._-]（客户端建议 UUID v4）
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")

# 默认请求者标识（单用户应用；6E API 层可注入真实标识）
DEFAULT_OWNER_ID = "default"


def _to_db_utc(value: datetime) -> datetime:
    """写入数据库前的 UTC 归一化（方言唯一时区接缝，见 core/dialect.py）。"""
    return coerce_utc_for_db(value, DIALECT)


def _validate_run_at(run_at: datetime) -> datetime:
    """6D UTC 规范校验：只接受 aware datetime。

    - 非 datetime 实例 → EXEC_RUN_AT_INVALID（类型错误，fail-closed）；
    - naive datetime → EXEC_RUN_AT_NAIVE（**拒绝，绝不静默解释**为本地或
      UTC 时间——naive 没有时区信息，解释成任何时区都是猜测）；
    - aware → 转为 UTC 落库（coerce_utc_for_db：SQLite naive-UTC / PG
      TIMESTAMPTZ），比较与存储保持 UTC 语义。
    """
    if not isinstance(run_at, datetime):
        raise ExecutionRunAtInvalidError()
    if run_at.tzinfo is None:
        raise ExecutionRunAtNaiveError()
    return run_at.astimezone(timezone.utc)  # aware UTC（事件 payload 用）


# ---- 状态机白名单 ----

# 合法状态转换表（from -> {to}）。终态（SUCCEEDED/FAILED/CANCELLED/TIMED_OUT）
# 无出边——不可逆。TIMED_OUT 源限定 {RUNNING, CANCEL_REQUESTED}（设计细化：
# CREATED/QUEUED 未开始执行不会超时；CLAIMED 超时 = lease 过期回收 QUEUED，
# 不是 TIMED_OUT——执行超时裁决只发生在执行中）。
ALLOWED_EXECUTION_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.CREATED: frozenset(
        {ExecutionStatus.QUEUED, ExecutionStatus.CANCELLED}
    ),
    ExecutionStatus.QUEUED: frozenset(
        {
            ExecutionStatus.CLAIMED,  # 6B 原子领取
            ExecutionStatus.CANCEL_REQUESTED,  # 取消请求（两跳路径）
            ExecutionStatus.CANCELLED,  # 未领取即取消：直接终态
        }
    ),
    ExecutionStatus.CLAIMED: frozenset(
        {
            ExecutionStatus.RUNNING,  # worker 确认开始
            ExecutionStatus.QUEUED,  # lease 过期回收（6B）
            ExecutionStatus.CANCEL_REQUESTED,
        }
    ),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.QUEUED,  # 可重试失败（6B 退避）
            ExecutionStatus.CANCEL_REQUESTED,
            ExecutionStatus.TIMED_OUT,  # 执行超时
        }
    ),
    ExecutionStatus.CANCEL_REQUESTED: frozenset(
        {
            ExecutionStatus.CANCELLED,
            ExecutionStatus.SUCCEEDED,  # 取消竞态：完成胜出
            ExecutionStatus.FAILED,
            ExecutionStatus.TIMED_OUT,  # 取消不合作导致超时
        }
    ),
    ExecutionStatus.SUCCEEDED: frozenset(),
    # 6E Retry API 授权的人工重试入口：FAILED → QUEUED 是规格八明确要求的
    # 合法转换（"成功 retry 后回到 QUEUED"），CAS 条件 UPDATE + 事件同事务，
    # 与其余转换同一路径——绝不另起第二套转换逻辑。
    ExecutionStatus.FAILED: frozenset({ExecutionStatus.QUEUED}),
    ExecutionStatus.CANCELLED: frozenset(),
    ExecutionStatus.TIMED_OUT: frozenset(),
}

# 状态转换 → 审计事件（Phase 6C：状态变更与事件同一事务写入）。
# CREATED 无出边事件（create_execution 写 EXECUTION_CREATED）；
# CLAIMED 由 claim_next 的原子领取写 EXECUTION_CLAIMED（不经 transition_to）；
# TIMED_OUT 由 check_timeouts 写 EXECUTION_TIMED_OUT；本表仅为经
# transition_to 走的路径提供默认事件。RETRY_SCHEDULED 由 Worker 显式传入
# （覆盖 QUEUED 的默认 EXECUTION_QUEUED）。
_EVENT_BY_TARGET: dict[ExecutionStatus, str] = {
    ExecutionStatus.QUEUED: "EXECUTION_QUEUED",
    ExecutionStatus.CLAIMED: "EXECUTION_CLAIMED",
    ExecutionStatus.RUNNING: "EXECUTION_STARTED",
    ExecutionStatus.SUCCEEDED: "EXECUTION_SUCCEEDED",
    ExecutionStatus.FAILED: "EXECUTION_FAILED",
    ExecutionStatus.CANCELLED: "EXECUTION_CANCELLED",
    ExecutionStatus.CANCEL_REQUESTED: "CANCEL_REQUESTED",
    ExecutionStatus.TIMED_OUT: "EXECUTION_TIMED_OUT",
    ExecutionStatus.CREATED: "EXECUTION_CREATED",
}


# ---- 查询 ----

def get_execution(db: Session, execution_id: int) -> Execution:
    execution = db.get(Execution, execution_id)
    if execution is None:
        raise ExecutionNotFoundError(execution_id)
    return execution


def _find_by_idempotency(
    db: Session, owner_id: str, execution_type: str, idempotency_key: str
) -> Execution | None:
    return (
        db.query(Execution)
        .filter(
            Execution.owner_id == owner_id,
            Execution.execution_type == execution_type,
            Execution.idempotency_key == idempotency_key,
        )
        .first()
    )


# ---- 幂等创建 ----

def _validate_idempotency_key(idempotency_key: str) -> None:
    if not isinstance(idempotency_key, str) or not IDEMPOTENCY_KEY_RE.match(
        idempotency_key
    ):
        raise InvalidIdempotencyKeyError(
            "Idempotency-Key must be 8-128 characters of [A-Za-z0-9._-]"
        )


def _resolve_replay(
    db: Session, existing: Execution, payload_hash: str
) -> Execution:
    """幂等裁决：同 key 同请求 → 重放已有；同 key 异请求 → 409 冲突。

    重放路径在返回对象上打内存标记 `_replayed`（仅 API 层读取，不落库）：
    幂等裁决与标志同源于 service，HTTP 层绝不重新实现幂等逻辑。
    """
    if existing.normalized_payload_hash == payload_hash:
        existing._replayed = True  # 6E：HTTP 层据此返回 200 + replayed 标志
        return existing  # 重放：返回已有资源（含当前状态）
    raise IdempotencyConflictError(existing.id, existing.idempotency_key)


def create_execution(
    db: Session,
    *,
    execution_type: str,
    payload: dict,
    idempotency_key: str,
    owner_id: str = DEFAULT_OWNER_ID,
    run_at: datetime | None = None,
    correlation_id: str | None = None,
    clock: Clock | None = None,
    commit: bool = True,
    allow_internal_type: bool = False,
) -> Execution:
    """创建 Execution（幂等）。

    - 同 owner + type + key、相同规范化请求 → 返回已有 Execution（重放）；
    - 同 key、不同请求 → IdempotencyConflictError（409）；
    - 并发重复：先查后写仅为快速路径，真实裁决是 UNIQUE 约束 +
      IntegrityError 捕获后重查比对（reminder 投递幂等同模式）；
    - run_at 仅存储，本阶段不做调度（状态保持 CREATED，需显式 enqueue）。
    - commit=False 只供需要把 receipt 与其他 domain rows 合并成一个事务的
      内部服务使用；默认行为与既有 API 完全一致。
    - internal_only 类型必须由明确的内部调用传 allow_internal_type=True；
      公共 Execution API 对它们返回 EXEC_TYPE_UNKNOWN。
    """
    _validate_idempotency_key(idempotency_key)
    # 注册表校验（未知 type → EXEC_TYPE_UNKNOWN）+ payload 白名单规范化
    # （敏感字段拒绝，fail-closed）→ 请求指纹
    type_spec = get_type_spec(
        execution_type, include_internal=allow_internal_type
    )
    normalized, payload_hash = normalize_payload(
        execution_type, payload, include_internal=allow_internal_type
    )
    now = _to_db_utc((clock or SystemClock()).now())

    existing = _find_by_idempotency(db, owner_id, execution_type, idempotency_key)
    if existing is not None:
        return _resolve_replay(db, existing, payload_hash)

    # 6D：run_at 校验在幂等检查之后（重放请求不得因重复调度报错）。
    # aware 必填（naive → EXEC_RUN_AT_NAIVE，绝不静默解释）；事件 payload
    # 用 aware UTC 值序列化（SQLite 落库后 tzinfo 已剥离）。
    run_at_utc: datetime | None = None
    if run_at is not None:
        run_at_utc = _validate_run_at(run_at)  # 先校验（fail-closed）再归一化
        run_at = _to_db_utc(run_at_utc)  # 方言落库值（SQLite naive-UTC）

    execution = Execution(
        owner_id=owner_id,
        execution_type=execution_type,
        status=ExecutionStatus.CREATED.value,
        payload=json.dumps(
            normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ),
        normalized_payload_hash=payload_hash,
        idempotency_key=idempotency_key,
        run_at=run_at,
        correlation_id=correlation_id,
        max_attempts=type_spec.max_attempts,
        timeout_seconds=type_spec.timeout_seconds,
        created_at=now,
    )
    db.add(execution)
    try:
        db.flush()  # 幂等键唯一约束裁决（并发重复提交在此抛出）
        # Phase 6C：EXECUTION_CREATED 与创建同一事务写入（审计起点；
        # 失败回滚则创建一并回滚，事件绝不悬空）
        append_event(
            db,
            execution_id=execution.id,
            event_type="EXECUTION_CREATED",
            actor_type="service",
            actor_id="execution_service",
            correlation_id=correlation_id,
            payload={"execution_type": execution_type, "owner_id": owner_id},
            clock=clock,
        )
        # 6D：带 run_at 的创建是调度声明——EXECUTION_SCHEDULED 与创建
        # 同一事务写入（调度裁决成功才写；run_at=None 不写）
        if run_at_utc is not None:
            append_event(
                db,
                execution_id=execution.id,
                event_type="EXECUTION_SCHEDULED",
                actor_type="service",
                actor_id="execution_service",
                correlation_id=correlation_id,
                payload={
                    "execution_type": execution_type,
                    "owner_id": owner_id,
                    "run_at": run_at_utc.isoformat(),
                },
                clock=clock,
            )
        if commit:
            db.commit()
    except IntegrityError:
        db.rollback()
        # 并发重复提交：唯一约束裁决。重查——存在则按哈希比对裁决
        # （重放或 409），绝不靠先查后写。
        existing = _find_by_idempotency(db, owner_id, execution_type, idempotency_key)
        if existing is not None:
            return _resolve_replay(db, existing, payload_hash)
        raise  # 其他数据错误（防御分支）：原样抛出
    if commit:
        db.refresh(execution)
    return execution


# ---- 状态转换 ----

def transition_to(
    db: Session,
    execution_id: int,
    target: ExecutionStatus,
    *,
    clock: Clock | None = None,
    event_type: str | None = None,
    event_payload: dict | None = None,
    commit: bool = True,
) -> Execution:
    """通用状态转换：白名单校验 + 条件 UPDATE（并发安全，rowcount 裁决）。

    终态不可逆：白名单中终态无出边，任何从终态出发的转换都会被拒；
    目标必须是白名单成员，否则 EXEC_INVALID_STATE_TRANSITION。

    Phase 6C：状态变更与审计事件**同一事务**写入——只有 CAS 真正获胜
    （rowcount==1）才追加事件；失败路径（并发竞争者已推进）不产生事件。
    event_type/event_payload 覆盖默认事件（如 Worker 重试路径显式写
    RETRY_SCHEDULED）。
    """
    execution = get_execution(db, execution_id)
    current = ExecutionStatus(execution.status)
    allowed = ALLOWED_EXECUTION_TRANSITIONS.get(current, frozenset())
    if target not in allowed:
        raise InvalidExecutionTransitionError(execution_id, current.value, target.value)

    now = _to_db_utc((clock or SystemClock()).now())
    values: dict = {"status": target.value}
    if target is ExecutionStatus.QUEUED and execution.queued_at is None:
        values["queued_at"] = now
    if target is ExecutionStatus.RUNNING and execution.started_at is None:
        values["started_at"] = now
    if target is ExecutionStatus.CANCEL_REQUESTED and execution.cancel_requested_at is None:
        values["cancel_requested_at"] = now
    if target in TERMINAL_STATUSES and execution.finished_at is None:
        values["finished_at"] = now

    result = db.execute(
        update(Execution)
        .where(Execution.id == execution_id, Execution.status == execution.status)
        .values(**values)
    )
    if result.rowcount == 0:
        db.rollback()
        db.refresh(execution)
        # 并发竞争者已推进：若已到达目标状态 → 幂等返回；否则按当前状态拒绝
        if execution.status == target.value:
            return execution
        current = ExecutionStatus(execution.status)
        raise InvalidExecutionTransitionError(execution_id, current.value, target.value)
    resolved_type = event_type or _EVENT_BY_TARGET[target]
    if resolved_type is not None:
        append_event(
            db,
            execution_id=execution_id,
            event_type=resolved_type,
            actor_type="service",
            actor_id="execution_service",
            correlation_id=execution.correlation_id,
            payload=event_payload,
            clock=clock,
        )
    if commit:
        db.commit()
        db.refresh(execution)
    else:
        db.flush()
        db.expire(execution)
    return execution


def enqueue(db: Session, execution_id: int, *, clock: Clock | None = None,
            commit: bool = True) -> Execution:
    """入队：CREATED → QUEUED（可被领取）。重复入队 → EXEC_INVALID_STATE_TRANSITION。"""
    return transition_to(db, execution_id, ExecutionStatus.QUEUED, clock=clock, commit=commit)


def request_cancel(
    db: Session, execution_id: int, *, clock: Clock | None = None
) -> Execution:
    """请求取消（协作式）。

    - CREATED / QUEUED：未开始执行，无 worker 需要配合 → 直接 CANCELLED；
    - CLAIMED / RUNNING：登记 CANCEL_REQUESTED（6B 的 worker 在 step 边界
      检查并确认放弃；本阶段仅登记，无执行逻辑）；
    - CANCEL_REQUESTED：幂等返回；
    - 终态：EXEC_INVALID_STATE_TRANSITION（已完成/失败/已取消/已超时不可取消）。
    """
    execution = get_execution(db, execution_id)
    current = ExecutionStatus(execution.status)
    if current in (ExecutionStatus.CREATED, ExecutionStatus.QUEUED):
        return transition_to(db, execution_id, ExecutionStatus.CANCELLED, clock=clock)
    if current in (ExecutionStatus.CLAIMED, ExecutionStatus.RUNNING):
        return transition_to(
            db, execution_id, ExecutionStatus.CANCEL_REQUESTED, clock=clock
        )
    if current is ExecutionStatus.CANCEL_REQUESTED:
        return execution  # 已请求取消：幂等
    raise InvalidExecutionTransitionError(
        execution_id, current.value, ExecutionStatus.CANCELLED.value
    )


def retry_execution(
    db: Session, execution_id: int, *, clock: Clock | None = None
) -> Execution:
    """人工重试（6E Retry API）：FAILED → QUEUED。

    - 幂等保护（规格八）完全由数据库状态/CAS 提供，绝不用内存缓存：
      并发双击时 transition_to 的条件 UPDATE 只有一个 rowcount==1 获胜并
      写 RETRY_SCHEDULED；失败方重查发现已到达 QUEUED → 幂等返回，绝不
      产生重复调度事件；本函数对 QUEUED 直接幂等返回（双击的第二次调用）；
    - 只允许 FAILED：其他状态（含其余终态与未失败任务）→
      EXEC_INVALID_STATE_TRANSITION（稳定 409）；
    - 不覆盖旧 attempt/step/event（append-only 审计）；新 attempt 由
      Worker/claim 决定；不改 run_at（不形成 recurring）；FAILED 终态时
      retry_after 恒 None（6B 失败路径不设退避），claim 门自然放行。
    """
    execution = get_execution(db, execution_id)
    current = ExecutionStatus(execution.status)
    if current is ExecutionStatus.QUEUED:
        return execution  # 已在队列（首次 retry 已生效）：幂等
    if current is not ExecutionStatus.FAILED:
        raise InvalidExecutionTransitionError(
            execution_id, current.value, ExecutionStatus.QUEUED.value
        )
    return transition_to(
        db,
        execution_id,
        ExecutionStatus.QUEUED,
        clock=clock,
        event_type="RETRY_SCHEDULED",
        event_payload={
            "attempt_number": execution.attempt_count,
            "reason": "api_retry",
        },
    )
