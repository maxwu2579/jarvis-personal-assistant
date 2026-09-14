"""原子 claim / lease / recovery / timeout 数据库操作（Phase 6B）。

全部并发裁决使用单语句条件 UPDATE（compare-and-set），与 PostgreSQL
SELECT ... FOR UPDATE SKIP LOCKED 等价（行锁 + WHERE 条件重评估在一条
UPDATE 内原子完成；两个并发 Worker 对同一行至多一个 rowcount=1），且
SQLite / PostgreSQL 走同一路径（SQLite 单写者天然串行，PG 由行锁支持
多 Worker）。

规则（沿已确认设计）：
- claim：WHERE 含 status='QUEUED' + run_at/retry_after 到期 → rowcount=1
  才在同一事务创建 attempt（编号 = attempt_count+1，UNIQUE 约束兜底）并
  递增 attempt_count；attempt 持有租约（worker_id/lease_token/lease_expires_at
  权威字段），execution 的 lease 三列是镜像（同事务写）；
- renew：WHERE 含 lease_token —— token 不匹配即 rowcount=0 → 租约已移交/
  回收，旧 Worker 后续写全部判定 EXEC_CLAIM_LOST 终止；
- 6D misfire：claim 前检测（run_at IS NOT NULL 且 attempt_count==0 且
  now > run_at + grace）→ 按 registry 策略裁决：RUN_IMMEDIATELY 照常 claim
  （MISFIRE_DETECTED + MISFIRE_RUN_IMMEDIATELY 与 claim 同事务写入）；
  SKIP → QUEUED→CANCELLED；FAIL → QUEUED→FAILED（last_error_code=
  EXEC_MISFIRE_EXPIRED）。全部条件 UPDATE rowcount 仲裁——misfire 与
  claim/cancel 绝不双赢；run_at=None 或已领取/重试过的任务永不 misfire；
- recover：只回收过期租约的活跃 attempt（CLAIMED/RUNNING）；终态
  execution 绝不被重新执行；CLAIMED → QUEUED，RUNNING → TIMED_OUT（已超时）
  或 QUEUED（未超时），CANCEL_REQUESTED → TIMED_OUT（已超时）；
- timeout：RUNNING / CANCEL_REQUESTED 且 started_at + timeout_seconds 已到
  → 条件 UPDATE 到 TIMED_OUT——与 Worker 完成互为单一终态裁决（行锁串行
  化后只有一个成功）。
"""

import uuid
from datetime import timedelta

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from app.core.clock import Clock, SystemClock
from app.core.database import DIALECT
from app.core.dialect import coerce_utc_for_db
from app.models.execution import TERMINAL_STATUSES, Execution, ExecutionStatus
from app.models.execution_attempt import (
    LIVE_ATTEMPT_STATUSES,
    AttemptStatus,
    ExecutionAttempt,
)
from app.services.execution_errors import (
    MISFIRE_EXPIRED_MESSAGE,
    ExecutionClaimLostError,
    ExecutionTypeUnknownError,
)
from app.services.execution_events import append_event
from app.services.execution_registry import PUBLIC_EXECUTION_TYPES, get_type_spec


def _to_db_utc(value):
    """写入数据库前的 UTC 归一化（方言唯一时区接缝，见 core/dialect.py）。"""
    return coerce_utc_for_db(value, DIALECT)


def _now(clock: Clock | None) -> "datetime":
    return _to_db_utc((clock or SystemClock()).now())


def _live_statuses() -> list[str]:
    return [s.value for s in LIVE_ATTEMPT_STATUSES]


def claim_next(
    db: Session,
    worker_id: str,
    *,
    clock: Clock | None = None,
    lease_ttl_seconds: int = 60,
) -> tuple[Execution, ExecutionAttempt] | None:
    """原子领取下一个可执行 execution。

    候选：QUEUED 且 run_at 到期且 retry_after 到期。单语句条件 UPDATE
    裁决并发：rowcount=1 才创建 attempt（同一事务），否则返回 None（竞争
    失败/无候选）。

    6D misfire（与 claim 同一咽喉点，CAS 单赢家）：
    - 候选已 misfire（run_at IS NOT NULL 且 attempt_count==0 且
      now > run_at + grace）→ 按 registry 策略裁决；SKIP/FAIL 直接判终态
      并返回 None（本轮不 claim）；
    - RUN_IMMEDIATELY → 照常 claim，MISFIRE_DETECTED + MISFIRE_RUN_IMMEDIATELY
      在 claim 事务内写入（claim CAS 失败则事件也不写——事件只在调度裁决
      成功后写）；
    - 到期 claim（run_at 非空）先写 SCHEDULE_DUE 再写 EXECUTION_CLAIMED。

    返回 (execution, attempt)，execution 已为 CLAIMED 且镜像租约已落盘。
    """
    now = _now(clock)
    candidate = (
        db.query(Execution)
        .filter(
            Execution.execution_type.in_(tuple(PUBLIC_EXECUTION_TYPES)),
            Execution.status == ExecutionStatus.QUEUED.value,
            or_(Execution.run_at.is_(None), Execution.run_at <= now),
            or_(Execution.retry_after.is_(None), Execution.retry_after <= now),
        )
        .order_by(Execution.id)
        .first()
    )
    if candidate is None:
        return None

    # 6D misfire 检测 + 裁决：SKIP/FAIL 在此判终态（条件 UPDATE rowcount
    # 仲裁——与并发 claim/cancel 绝不双赢）；RUN_IMMEDIATELY 继续 claim。
    misfire = _misfire_state(candidate, now)
    if misfire is not None:
        policy, grace = misfire
        if _resolve_misfire(db, candidate, policy, grace, now, clock):
            return None

    lease_token = uuid.uuid4().hex
    lease_expires = now + timedelta(seconds=lease_ttl_seconds)

    # 单语句原子 claim（CAS）：与 SELECT ... FOR UPDATE SKIP LOCKED 等价。
    # execution 只镜像 owner + expires（6A 建列）；lease_token 权威在 attempt。
    result = db.execute(
        update(Execution)
        .where(
            Execution.id == candidate.id,
            Execution.execution_type.in_(tuple(PUBLIC_EXECUTION_TYPES)),
            Execution.status == ExecutionStatus.QUEUED.value,
            or_(Execution.run_at.is_(None), Execution.run_at <= now),
            or_(Execution.retry_after.is_(None), Execution.retry_after <= now),
        )
        .values(
            status=ExecutionStatus.CLAIMED.value,
            lease_owner=worker_id,
            lease_expires_at=lease_expires,
            updated_at=now,
        )
    )
    if result.rowcount == 0:
        return None  # 竞争失败：其他 worker 已领取或状态已变

    # 同一事务：创建 attempt（编号唯一由 UNIQUE 约束兜底）并递增 attempt_count
    attempt_number = candidate.attempt_count + 1
    attempt = ExecutionAttempt(
        execution_id=candidate.id,
        attempt_number=attempt_number,
        worker_id=worker_id,
        lease_token=lease_token,
        lease_expires_at=lease_expires,
        status=AttemptStatus.CLAIMED.value,
        created_at=now,
        updated_at=now,
    )
    db.add(attempt)
    db.execute(
        update(Execution)
        .where(Execution.id == candidate.id)
        .values(attempt_count=attempt_number, updated_at=now)
    )
    # 6D 事件顺序（全部与领取同一事务）：
    #   run_at 非空（调度任务到期）→ SCHEDULE_DUE（调度裁决成功）；
    #   misfire RUN_IMMEDIATELY → MISFIRE_DETECTED + MISFIRE_RUN_IMMEDIATELY；
    #   EXECUTION_CLAIMED（6C：审计租约归属；事件序列冲突 → 整个领取
    #   回滚，execution 保持 QUEUED 自愈）
    if candidate.run_at is not None:
        append_event(
            db,
            execution_id=candidate.id,
            event_type="SCHEDULE_DUE",
            attempt_id=attempt.id,
            actor_type="worker",
            actor_id=worker_id,
            correlation_id=candidate.correlation_id,
            payload={"run_at": candidate.run_at.isoformat()},
            clock=clock,
        )
    if misfire is not None:
        policy, grace = misfire  # 此处仅 RUN_IMMEDIATELY（SKIP/FAIL 已返回）
        append_event(
            db,
            execution_id=candidate.id,
            event_type="MISFIRE_DETECTED",
            attempt_id=attempt.id,
            actor_type="worker",
            actor_id=worker_id,
            correlation_id=candidate.correlation_id,
            payload={
                "run_at": candidate.run_at.isoformat(),
                "misfire_grace_seconds": grace,
            },
            clock=clock,
        )
        append_event(
            db,
            execution_id=candidate.id,
            event_type="MISFIRE_RUN_IMMEDIATELY",
            attempt_id=attempt.id,
            actor_type="worker",
            actor_id=worker_id,
            correlation_id=candidate.correlation_id,
            payload={"run_at": candidate.run_at.isoformat()},
            clock=clock,
        )
    # Phase 6C：EXECUTION_CLAIMED 与领取同一事务写入（审计租约归属；
    # 事件序列冲突 → 整个领取回滚，execution 保持 QUEUED 自愈）
    append_event(
        db,
        execution_id=candidate.id,
        event_type="EXECUTION_CLAIMED",
        attempt_id=attempt.id,
        actor_type="worker",
        actor_id=worker_id,
        correlation_id=candidate.correlation_id,
        payload={
            "worker_id": worker_id,
            "attempt_number": attempt_number,
            "lease_expires_at": lease_expires.isoformat(),
        },
        clock=clock,
    )
    db.commit()
    db.refresh(candidate)
    return candidate, attempt


def renew_lease(
    db: Session,
    execution: Execution,
    attempt: ExecutionAttempt,
    *,
    clock: Clock | None = None,
    lease_ttl_seconds: int = 60,
) -> None:
    """租约续期（心跳）。WHERE 含 lease_token：租约已被回收/移交 → 抛
    EXEC_CLAIM_LOST，Worker 必须立即终止处理。

    attempt（权威）与 execution（镜像）同事务续期。
    """
    now = _now(clock)
    lease_expires = now + timedelta(seconds=lease_ttl_seconds)
    result = db.execute(
        update(ExecutionAttempt)
        .where(
            ExecutionAttempt.id == attempt.id,
            ExecutionAttempt.lease_token == attempt.lease_token,
            ExecutionAttempt.status.in_(_live_statuses()),
        )
        .values(lease_expires_at=lease_expires, updated_at=now)
    )
    # execution 镜像同步（同事务）：owner 匹配才续期（recover 已清空 →
    # 镜像不再更新；attempt 行是权威，rowcount 判定仍以 attempt 为准）
    db.execute(
        update(Execution)
        .where(
            Execution.id == execution.id,
            Execution.lease_owner == attempt.worker_id,
        )
        .values(lease_expires_at=lease_expires, updated_at=now)
    )
    db.commit()
    if result.rowcount == 0:
        raise ExecutionClaimLostError(execution.id, attempt.id)


def recover_expired(db: Session, *, clock: Clock | None = None) -> int:
    """回收过期租约（Worker 崩溃 / lease 过期后的安全接管）。

    - CLAIMED 且租约过期 → QUEUED（重新排队；attempt → ABORTED 保留审计）
    - RUNNING 且租约过期 → 已超时则 TIMED_OUT，否则 QUEUED（租约丢失重排）
    - CANCEL_REQUESTED 且租约过期 → 已超时则 TIMED_OUT
    - 终态 execution 绝不回收（已完成绝不重新执行）
    返回回收（变更）的 execution 数。
    """
    now = _now(clock)
    recovered = 0
    live = (
        db.query(ExecutionAttempt)
        .filter(
            ExecutionAttempt.status.in_(_live_statuses()),
            ExecutionAttempt.lease_expires_at < now,
        )
        .order_by(ExecutionAttempt.id)
        .all()
    )
    for attempt in live:
        execution = db.get(Execution, attempt.execution_id)
        if execution is None or execution.execution_type not in PUBLIC_EXECUTION_TYPES:
            continue  # internal receipts never enter recovery/requeue lifecycle
        if execution.status in {
            s.value for s in TERMINAL_STATUSES
        }:
            continue  # 终态 execution 不得被重新执行

        if _abort_attempt(db, attempt.id, now) == 0:
            continue  # 并发回收已处理（rowcount=0），本循环无待提交内容
        status = execution.status
        if status == ExecutionStatus.CLAIMED.value:
            _requeue(db, execution, now)
            recovered += 1
        elif status == ExecutionStatus.RUNNING.value:
            if _timeout_due(execution, now):
                _timeout_execution(db, execution, now)
            else:
                _requeue(db, execution, now)
            recovered += 1
        elif status == ExecutionStatus.CANCEL_REQUESTED.value:
            if _timeout_due(execution, now):
                _timeout_execution(db, execution, now)
                recovered += 1
        # Phase 6C：LEASE_RECOVERED 与 abort/requeue 同一事务提交（审计
        # 接管；事件绝不悬空）
        append_event(
            db,
            execution_id=execution.id,
            event_type="LEASE_RECOVERED",
            attempt_id=attempt.id,
            actor_type="service",
            actor_id="execution_claim",
            correlation_id=execution.correlation_id,
            payload={
                "worker_id": attempt.worker_id,
                "attempt_number": attempt.attempt_number,
            },
            clock=clock,
        )
        db.commit()
        # CREATED/QUEUED/终态 无活跃 attempt：跳过
    return recovered


def check_timeouts(db: Session, *, clock: Clock | None = None) -> int:
    """运行超时裁决：RUNNING / CANCEL_REQUESTED 且超时已到 → TIMED_OUT。

    与 Worker 完成互为单一终态裁决：双方都是条件 UPDATE（WHERE status），
    行锁串行化后只有一个成功——timeout 与 success race 不会出现双终态。
    活跃 attempt 同步 ABORTED（审计保留）。返回超时数。
    """
    now = _now(clock)
    timed_out = 0
    due = (
        db.query(Execution)
        .filter(
            Execution.execution_type.in_(tuple(PUBLIC_EXECUTION_TYPES)),
            Execution.status.in_(
                [
                    ExecutionStatus.RUNNING.value,
                    ExecutionStatus.CANCEL_REQUESTED.value,
                ]
            ),
            Execution.started_at.is_not(None),
            Execution.timeout_seconds.is_not(None),
        )
        .all()
    )
    for execution in due:
        if (
            execution.started_at + timedelta(seconds=execution.timeout_seconds)
            > now
        ):
            continue
        result = db.execute(
            update(Execution)
            .where(Execution.id == execution.id, Execution.status == execution.status)
            .values(
                status=ExecutionStatus.TIMED_OUT.value,
                finished_at=now,
                updated_at=now,
            )
        )
        if result.rowcount == 0:
            db.rollback()
            continue  # 竞争胜出者（如完成）已推进状态
        # 活跃 attempt 同步 ABORTED（审计保留）；EXECUTION_TIMED_OUT 与
        # 终态同一事务写入（事件绝不悬空）
        live_attempts = (
            db.query(ExecutionAttempt)
            .filter(
                ExecutionAttempt.execution_id == execution.id,
                ExecutionAttempt.status.in_(_live_statuses()),
            )
            .order_by(ExecutionAttempt.id)
            .all()
        )
        for attempt in live_attempts:
            _abort_attempt(db, attempt.id, now)
        append_event(
            db,
            execution_id=execution.id,
            event_type="EXECUTION_TIMED_OUT",
            attempt_id=(live_attempts[0].id if live_attempts else None),
            actor_type="service",
            actor_id="execution_claim",
            correlation_id=execution.correlation_id,
            clock=clock,
        )
        db.commit()
        timed_out += 1
    return timed_out


# ---- 内部辅助（全部条件 UPDATE，绝不无条件改写） ----


def _misfire_state(execution: Execution, now) -> tuple[str, float] | None:
    """misfire 检测：run_at IS NOT NULL 且 attempt_count==0 且
    now > run_at + misfire_grace_seconds。

    - run_at=None 永远不属于 misfire；
    - attempt_count==0：只有**从未被领取**的任务才可能 misfire——retry /
      lease 恢复重排的任务已开始过，由 retry_after 门控，不算 misfire；
    - 边界：now == run_at + grace 恰好不 misfire（严格大于）；
    - registry 解析失败（type 被移除）→ 保守视为不 misfire（不因调度问题
      阻止执行，执行路径另有 fail-closed 裁决）。
    返回 (policy, grace_seconds) 或 None。
    """
    if execution.run_at is None or execution.attempt_count != 0:
        return None
    try:
        spec = get_type_spec(execution.execution_type)
    except ExecutionTypeUnknownError:
        return None
    grace = float(spec.misfire_grace_seconds)
    if now <= execution.run_at + timedelta(seconds=grace):
        return None
    return spec.misfire_policy, grace


def _resolve_misfire(
    db: Session,
    execution: Execution,
    policy: str,
    grace: float,
    now,
    clock: Clock | None,
) -> bool:
    """SKIP/FAIL 的 misfire 终态裁决（单语句条件 UPDATE，rowcount 仲裁）。

    - WHERE id + status='QUEUED'：与并发 claim / cancel 同一仲裁路径——
      竞争方胜出则 rowcount=0，本裁决不写任何事件（misfire 与 claim/
      cancel 绝不双赢）；
    - SKIP：→ CANCELLED（从未执行且永不再执行，与直接取消同义；不新增
      未批准状态）；
    - FAIL：→ FAILED + last_error_code=EXEC_MISFIRE_EXPIRED + 稳定脱敏文案；
    - 事件 MISFIRE_DETECTED + MISFIRE_SKIPPED/FAILED 与终态同一事务提交。

    返回 True = 已裁决（或竞争失败不再处理）；False = RUN_IMMEDIATELY
    （调用方继续正常 claim，misfire 事件在 claim 事务内写）。
    """
    if policy == "RUN_IMMEDIATELY":
        return False
    values: dict = {
        "status": (
            ExecutionStatus.CANCELLED.value
            if policy == "SKIP"
            else ExecutionStatus.FAILED.value
        ),
        "finished_at": now,
        "updated_at": now,
    }
    if policy == "FAIL":
        values["last_error_code"] = "EXEC_MISFIRE_EXPIRED"
        values["last_error_message"] = MISFIRE_EXPIRED_MESSAGE
    result = db.execute(
        update(Execution)
        .where(
            Execution.id == execution.id,
            Execution.status == ExecutionStatus.QUEUED.value,
        )
        .values(**values)
    )
    if result.rowcount == 0:
        db.rollback()  # 竞争方已推进（claim/cancel 胜出）——本裁决不写事件
        return True
    run_at_iso = execution.run_at.isoformat() if execution.run_at else None
    append_event(
        db,
        execution_id=execution.id,
        event_type="MISFIRE_DETECTED",
        actor_type="service",
        actor_id="execution_claim",
        correlation_id=execution.correlation_id,
        payload={"run_at": run_at_iso, "misfire_grace_seconds": grace},
        clock=clock,
    )
    append_event(
        db,
        execution_id=execution.id,
        event_type="MISFIRE_SKIPPED" if policy == "SKIP" else "MISFIRE_FAILED",
        actor_type="service",
        actor_id="execution_claim",
        correlation_id=execution.correlation_id,
        payload={"run_at": run_at_iso},
        clock=clock,
    )
    db.commit()
    return True


def _timeout_due(execution: Execution, now) -> bool:
    timeout = execution.timeout_seconds
    started = execution.started_at
    if timeout is None or started is None:
        return False
    return started + timedelta(seconds=timeout) <= now


def _abort_attempt(db: Session, attempt_id: int, now) -> int:
    """条件 UPDATE attempt → ABORTED。返回受影响行数（不提交——调用方把
    abort 与状态变更/事件放在同一事务提交）。rowcount=0 = 已被并发回收。"""
    result = db.execute(
        update(ExecutionAttempt)
        .where(
            ExecutionAttempt.id == attempt_id,
            ExecutionAttempt.status.in_(_live_statuses()),
        )
        .values(
            status=AttemptStatus.ABORTED.value,
            finished_at=now,
            updated_at=now,
        )
    )
    return int(result.rowcount or 0)


def _requeue(db: Session, execution: Execution, now) -> None:
    """CLIMED/RUNNING → QUEUED：清除租约，重新排队可被再领取。
    不提交——调用方与 LEASE_RECOVERED 事件同一事务提交。"""
    db.execute(
        update(Execution)
        .where(
            Execution.id == execution.id,
            Execution.status == execution.status,  # 条件：状态未被竞争推进才回收
        )
        .values(
            status=ExecutionStatus.QUEUED.value,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
        )
    )


def _timeout_execution(db: Session, execution: Execution, now) -> None:
    """条件 UPDATE 到 TIMED_OUT（状态未变才生效；失败则竞争方已推进）。
    不提交——调用方与事件同一事务提交。"""
    db.execute(
        update(Execution)
        .where(
            Execution.id == execution.id,
            Execution.status == execution.status,
        )
        .values(
            status=ExecutionStatus.TIMED_OUT.value,
            finished_at=now,
            updated_at=now,
        )
    )
