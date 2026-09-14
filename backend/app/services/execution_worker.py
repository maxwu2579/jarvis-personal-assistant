"""Execution Worker 独立进程（Phase 6B）。

用法（沿 reminder_worker 模式）：
    python -m app.services.execution_worker --once          # 单轮（测试/CI）
    python -m app.services.execution_worker --worker-id w1  # 无限轮询
    python -m app.services.execution_worker --once --now 2026-08-20T10:00:00Z

每 tick 轮循环节：
1. recover_expired —— 回收过期租约（Worker 崩溃恢复；终态 execution 绝
   不被重新执行，旧 attempt 保留为审计）；
2. check_timeouts —— 运行超时裁决（与完成互为单一终态裁决）；
3. renew 自身活跃 attempt —— 心跳续期（token 不匹配即 EXEC_CLAIM_LOST，
   立即终止处理）；
4. claim_next —— 原子领取（CAS；SQLite 单 Worker 由启动检查 + 行锁保证，
   PostgreSQL 由行锁支持多 Worker）；
5. 执行步骤 —— 6C 编排器按静态模板线性执行（只调用 registry 白名单函数；
   绝不执行任意 shell / 任意函数 / 模型生成的代码）→ 成功 SUCCEEDED；
   失败按工具 retryable 策略 + 确定性指数退避重试或耗尽 max_attempts 后
   FAILED；cancel 请求协作确认 CANCELLED；stale worker 的租约在每次写入
   边界校验，第一时间终止。

SQLite 单 Worker 限制：启动时检测到其他 worker 的活跃租约 → 拒绝启动
（EXEC_WORKER_CONFLICT）。PostgreSQL 支持多 Worker 并发 claim。

退出码：0 成功；1 启动/配置失败。
"""

import argparse
import json
import logging
import signal
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.clock import FixedClock, SystemClock
from app.core.database import DIALECT
from app.models.execution import Execution, ExecutionStatus
from app.models.execution_attempt import (
    LIVE_ATTEMPT_STATUSES,
    AttemptStatus,
    ExecutionAttempt,
)
from app.services.execution_claim import (
    check_timeouts,
    claim_next,
    recover_expired,
    renew_lease,
)
from app.services.execution_errors import (
    ExecutionClaimLostError,
    ExecutionStepError,
    ExecutionWorkerConflictError,
    InvalidExecutionTransitionError,
)
from app.services.execution_registry import backoff_seconds, get_type_spec
from app.services.execution_service import transition_to
from app.services.execution_steps import run_execution_steps
from app.core.sanitize import sanitize_error_message

logger = logging.getLogger("execution_worker")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise SystemExit(f"非法时间格式（需要带时区）：{value!r}")
    return parsed.astimezone(timezone.utc)


class ExecutionWorker:
    """单进程 Worker：循环 tick，处理 system.ping（注册表内）等 execution。"""

    def __init__(
        self,
        session_factory,
        *,
        worker_id: str | None = None,
        clock: "Clock | None" = None,
        lease_ttl_seconds: int = 60,
        poll_interval: float = 1.0,
    ):
        self._session_factory = session_factory
        self._worker_id = worker_id or f"worker-{uuid.uuid4().hex[:8]}"
        self._clock = clock or SystemClock()
        self._lease_ttl = lease_ttl_seconds
        self._poll_interval = poll_interval
        self._check_single_worker()

    # ---- 单 Worker 限制（SQLite） ----

    def _check_single_worker(self) -> None:
        """SQLite 只支持单 Worker：存在其他 worker 的活跃租约即拒绝启动。"""
        if DIALECT != "sqlite":
            return
        db = self._session_factory()
        try:
            holder = (
                db.query(ExecutionAttempt)
                .filter(
                    ExecutionAttempt.status.in_(
                        [s.value for s in LIVE_ATTEMPT_STATUSES]
                    ),
                    ExecutionAttempt.worker_id != self._worker_id,
                    ExecutionAttempt.lease_expires_at > self._now(),
                )
                .first()
            )
            if holder is not None:
                raise ExecutionWorkerConflictError(
                    self._worker_id, holder.worker_id
                )
        finally:
            db.close()

    # ---- 循环 ----

    def run_once(self, *, max_claims: int = 1) -> dict:
        """单轮 tick：recover → timeout → renew → claim → 执行。返回统计。"""
        stats = {
            "recovered": 0,
            "timed_out": 0,
            "renewed": 0,
            "claimed": 0,
            "succeeded": 0,
            "failed": 0,
            "retried": 0,
            "cancelled": 0,
            "aborted": 0,
            "skipped": 0,
        }
        db = self._session_factory()
        try:
            stats["recovered"] = recover_expired(db, clock=self._clock)
            stats["timed_out"] = check_timeouts(db, clock=self._clock)
            stats["renewed"] = self._renew_own(db)
            for _ in range(max_claims):
                claimed = claim_next(
                    db, self._worker_id, clock=self._clock,
                    lease_ttl_seconds=self._lease_ttl,
                )
                if claimed is None:
                    break
                execution, attempt = claimed
                stats["claimed"] += 1
                outcome = self._run_claimed(db, execution, attempt)
                stats[outcome] = stats.get(outcome, 0) + 1
        except ExecutionClaimLostError as exc:
            db.rollback()
            logger.warning(
                "execution_worker.claim_lost execution=%s message=%s",
                exc.execution_id if hasattr(exc, "execution_id") else "?",
                sanitize_error_message(exc.message),
            )
        finally:
            db.close()
        return stats

    def run_forever(self, *, stop_event=None) -> None:
        """无限轮询（SIGINT/SIGTERM 优雅退出；stop_event 供测试注入）。"""
        logger.info(
            "execution_worker.start worker=%s lease_ttl=%ss interval=%ss",
            self._worker_id,
            self._lease_ttl,
            self._poll_interval,
        )
        stop = {"flag": False}

        def _handle(signum, frame):
            logger.info("execution_worker.shutdown signal=%s", signum)
            stop["flag"] = True

        if stop_event is None:
            signal.signal(signal.SIGINT, _handle)
            signal.signal(signal.SIGTERM, _handle)
        while not stop["flag"]:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - Worker 循环不允许崩溃
                logger.error(
                    "execution_worker.tick_error type=%s message=%s",
                    type(exc).__name__,
                    sanitize_error_message(str(exc)),
                )
            if stop_event is not None and stop_event.is_set():
                break
            for _ in range(int(self._poll_interval)):
                if stop["flag"] or (stop_event is not None and stop_event.is_set()):
                    return
                time.sleep(1)
        logger.info("execution_worker.stopped worker=%s", self._worker_id)

    # ---- 内部 ----

    def _now(self):
        from app.core.dialect import coerce_utc_for_db

        return coerce_utc_for_db(self._clock.now(), DIALECT)

    def _renew_own(self, db: Session) -> int:
        """续期本 worker 仍活跃的 attempt（心跳）。token 失效 → 抛
        EXEC_CLAIM_LOST（租约已被回收，终止处理）。"""
        renewed = 0
        own = (
            db.query(ExecutionAttempt)
            .filter(
                ExecutionAttempt.worker_id == self._worker_id,
                ExecutionAttempt.status.in_(
                    [s.value for s in LIVE_ATTEMPT_STATUSES]
                ),
            )
            .all()
        )
        for attempt in own:
            execution = db.get(Execution, attempt.execution_id)
            if execution is None:
                continue
            renew_lease(
                db, execution, attempt,
                clock=self._clock, lease_ttl_seconds=self._lease_ttl,
            )
            renewed += 1
        return renewed

    def _run_claimed(
        self, db: Session, execution: Execution, attempt: ExecutionAttempt
    ) -> str:
        """执行一个已领取的 execution。返回结果类别（succeeded/failed/
        retried/cancelled/aborted）。"""
        # 1) CLAIMED → RUNNING（条件 UPDATE；若已被取消/回收则转向）
        try:
            transition_to(db, execution.id, ExecutionStatus.RUNNING, clock=self._clock)
        except InvalidExecutionTransitionError:
            return self._cancel_or_abort(db, execution, attempt)
        db.refresh(execution)

        # 2) 执行前 cancel 检查：请求了取消 → 确认 CANCELLED，不执行
        if execution.status == ExecutionStatus.CANCEL_REQUESTED.value:
            return self._confirm_cancel(db, execution, attempt)

        # 3) 按模板编排步骤（6C：注册表白名单工具；输出已脱敏；legacy
        #    type 合成单步，行为与 6B 逐字节等价）。每次写入边界都做
        #    lease 校验——stale worker 在第一次写入即 EXEC_CLAIM_LOST
        #    终止，绝不写 step/event/终态。
        try:
            result = run_execution_steps(db, execution, attempt, clock=self._clock)
            result_text = json.dumps(
                result,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except ExecutionStepError as exc:
            # 步骤失败：按工具 retryable 策略交给既有 attempt 重试机制
            return self._handle_failure(
                db, execution, attempt,
                exc.code, sanitize_error_message(exc.message),
                retryable=exc.retryable,
            )
        except ExecutionClaimLostError:
            # 租约丢失：立即终止处理（回收方已接管），绝不写任何状态
            return "aborted"
        except Exception as exc:  # noqa: BLE001 - handler 失败按执行失败处理
            return self._handle_failure(
                db, execution, attempt,
                getattr(exc, "code", None) or "EXEC_HANDLER_ERROR",
                sanitize_error_message(str(exc)),
            )

        # 4) 成功收尾：execution RUNNING→SUCCEEDED（失败则取消竞态裁决）
        try:
            transition_to(
                db, execution.id, ExecutionStatus.SUCCEEDED, clock=self._clock
            )
        except InvalidExecutionTransitionError:
            db.refresh(execution)
            if execution.status == ExecutionStatus.CANCEL_REQUESTED.value:
                # 取消竞态：完成胜出（状态机允许 CANCEL_REQUESTED→SUCCEEDED）
                try:
                    transition_to(
                        db, execution.id, ExecutionStatus.SUCCEEDED,
                        clock=self._clock,
                    )
                except InvalidExecutionTransitionError:
                    return self._cancel_or_abort(db, execution, attempt)
            else:
                return self._cancel_or_abort(db, execution, attempt)
        self._finish_attempt(
            db, attempt, AttemptStatus.SUCCEEDED, self._now(),
            result_text=result_text,
        )
        return "succeeded"

    def _handle_failure(
        self, db: Session, execution: Execution, attempt: ExecutionAttempt,
        error_code: str, error_message: str,
        retryable: bool = True,
    ) -> str:
        """失败处理：retryable 且未耗尽 → 退避重试（RUNNING→QUEUED +
        retry_after + RETRY_SCHEDULED 事件）；否则 → FAILED（终态）。
        旧 attempt 保留为审计。"""
        now = self._now()
        try:
            spec = get_type_spec(execution.execution_type)
            max_attempts = spec.max_attempts
        except Exception:  # noqa: BLE001 - type 被移除等：视为不可重试
            max_attempts = attempt.attempt_number
        try:
            if retryable and attempt.attempt_number < max_attempts:
                retry_after = now + timedelta(
                    seconds=backoff_seconds(spec, attempt.attempt_number)
                )
                transition_to(
                    db, execution.id, ExecutionStatus.QUEUED, clock=self._clock,
                    event_type="RETRY_SCHEDULED",
                    event_payload={
                        "attempt_number": attempt.attempt_number,
                        "error_code": error_code,
                        "error_message": error_message,
                    },
                )
                db.execute(
                    update(Execution)
                    .where(Execution.id == execution.id)
                    .values(
                        retry_after=retry_after,
                        last_error_code=error_code,
                        last_error_message=error_message,
                        updated_at=now,
                    )
                )
                self._finish_attempt(
                    db, attempt, AttemptStatus.FAILED, now,
                    error_code=error_code, error_message=error_message,
                )
                db.commit()
                return "retried"
            transition_to(
                db, execution.id, ExecutionStatus.FAILED, clock=self._clock
            )
            db.execute(
                update(Execution)
                .where(Execution.id == execution.id)
                .values(
                    last_error_code=error_code,
                    last_error_message=error_message,
                    updated_at=now,
                )
            )
            self._finish_attempt(
                db, attempt, AttemptStatus.FAILED, now,
                error_code=error_code, error_message=error_message,
            )
            db.commit()
            return "failed"
        except InvalidExecutionTransitionError:
            return self._cancel_or_abort(db, execution, attempt)

    def _confirm_cancel(
        self, db: Session, execution: Execution, attempt: ExecutionAttempt
    ) -> str:
        """Worker 协作确认取消：CANCEL_REQUESTED → CANCELLED（条件 UPDATE，
        与完成竞态单一终态）。attempt ABORTED（审计）。"""
        now = self._now()
        try:
            transition_to(
                db, execution.id, ExecutionStatus.CANCELLED, clock=self._clock
            )
        except InvalidExecutionTransitionError:
            return self._cancel_or_abort(db, execution, attempt)
        self._finish_attempt(
            db, attempt, AttemptStatus.ABORTED, now, error_code="EXEC_CANCELLED"
        )
        return "cancelled"

    def _cancel_or_abort(
        self, db: Session, execution: Execution, attempt: ExecutionAttempt
    ) -> str:
        """状态已被竞争方推进：若处于 CANCEL_REQUESTED → 确认取消；否则
        仅将 attempt 收尾为 ABORTED（租约丢失/已终态），绝不复写 execution。"""
        db.refresh(execution)
        if execution.status == ExecutionStatus.CANCEL_REQUESTED.value:
            return self._confirm_cancel(db, execution, attempt)
        self._finish_attempt(
            db, attempt, AttemptStatus.ABORTED, self._now()
        )
        return "aborted"

    def _finish_attempt(
        self, db: Session, attempt: ExecutionAttempt, status: AttemptStatus,
        now, *, result_text: str | None = None,
        error_code: str | None = None, error_message: str | None = None,
    ) -> None:
        """attempt 终态收尾（条件 UPDATE：租约仍由本 token 持有且活跃）。
        失败（token 不匹配/已终态）→ EXEC_CLAIM_LOST。"""
        result = db.execute(
            update(ExecutionAttempt)
            .where(
                ExecutionAttempt.id == attempt.id,
                ExecutionAttempt.lease_token == attempt.lease_token,
                ExecutionAttempt.status.in_(
                    [s.value for s in LIVE_ATTEMPT_STATUSES]
                ),
            )
            .values(
                status=status.value,
                result_text=result_text,
                error_code=error_code,
                error_message=error_message,
                finished_at=now,
                updated_at=now,
            )
        )
        db.commit()
        if result.rowcount == 0:
            raise ExecutionClaimLostError(attempt.execution_id, attempt.id)


def _safe_tick(worker: ExecutionWorker) -> None:
    """单轮处理；任何异常都不让 Worker 循环崩溃，且日志经过脱敏。"""
    try:
        stats = worker.run_once()
        logger.info("execution_worker.tick worker=%s stats=%s", worker._worker_id, stats)
    except Exception as exc:  # noqa: BLE001 - Worker 循环不允许崩溃
        logger.error(
            "execution_worker.error type=%s message=%s",
            type(exc).__name__,
            sanitize_error_message(str(exc)),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="JARVIS Execution Worker")
    parser.add_argument("--once", action="store_true", help="只处理一轮后退出")
    parser.add_argument(
        "--worker-id", default=None, help="Worker 标识（默认自动生成）"
    )
    parser.add_argument("--lease-ttl", type=int, default=60, help="租约时长（秒）")
    parser.add_argument(
        "--now", default=None,
        help="固定当前时间（ISO 8601 带时区）；仅测试/演示用",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        from app.core.database import SessionLocal

        clock = FixedClock(_parse_iso(args.now)) if args.now else SystemClock()
        worker = ExecutionWorker(
            SessionLocal,
            worker_id=args.worker_id,
            clock=clock,
            lease_ttl_seconds=args.lease_ttl,
        )
        if args.once:
            print(f"execution_worker.once stats={worker.run_once()}")
        else:
            worker.run_forever()
    except (ExecutionWorkerConflictError, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - 启动失败给出干净退出码
        logger.error(
            "execution_worker.startup_failed type=%s message=%s",
            type(exc).__name__,
            sanitize_error_message(str(exc)),
        )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    sys.exit(main())
