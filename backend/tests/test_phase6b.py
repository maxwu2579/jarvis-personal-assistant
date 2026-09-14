"""Phase 6B 定向测试：Durable Execution Worker and Atomic Claiming。

覆盖需求清单（20 项）+ 迁移回环 + PG 离线 SQL + 单 head 链验证。
全部离线：临时 SQLite + FakeClock（可推进），无在线模型 / 外部服务。

fixture 沿 test_phase6a 模式：tmp_path 独立 SQLite + FK pragma + create_all。
"""

import itertools
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.config import settings
from app.core.clock import FixedClock
from app.core.database import Base, _set_sqlite_pragma
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
    ExecutionWorkerConflictError,
)
from app.services.execution_registry import (
    EXECUTION_HANDLERS,
    EXECUTION_TYPES,
    ExecutionTypeSpec,
    backoff_seconds,
)
from app.services.execution_service import (
    create_execution,
    enqueue,
    get_execution,
    request_cancel,
    transition_to,
)
from app.services.execution_worker import ExecutionWorker
from app.core.sanitize import sanitize_error_message, sanitize_result_value

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
KEY = "test-key-0001"
PING = {}

# 可推进时钟（6B 需要模拟 lease 过期 / timeout / backoff 时间流逝）
class FakeClock:
    def __init__(self, start=FIXED_NOW):
        self._now = start

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)


_key_counter = itertools.count(1)


def _unique_key() -> str:
    return f"test-key-{next(_key_counter):08d}"


def _naive(dt):
    """SQLite 下 DB 时间为 naive UTC；断言时统一。"""
    return dt.replace(tzinfo=None)


@pytest.fixture()
def db_session(tmp_path):
    db_file = tmp_path / "test_6b.db"
    engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine
    )
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
    engine.dispose()


def _session_factory(db_session):
    """同一临时库的独立 session 工厂（模拟独立 Worker 进程）。"""
    engine = db_session.get_bind()
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _clock():
    return FixedClock(FIXED_NOW)


def _make(db, key=None, payload=PING, **kwargs):
    return create_execution(
        db,
        execution_type="system.ping",
        payload=payload,
        idempotency_key=key or _unique_key(),
        clock=_clock(),
        **kwargs,
    )


def _walk(db, execution, *statuses):
    for target in statuses:
        execution = transition_to(db, execution.id, target, clock=_clock())
    return execution


def _claim(db, worker_id="worker-a", clock=None):
    return claim_next(db, worker_id, clock=clock or _clock(), lease_ttl_seconds=60)


def _register(db_session, name, *, max_attempts=3, timeout_seconds=None,
              base=0.001, factor=2.0, handler=None):
    """monkeypatch 级注册测试 type（临时，fixture 自动恢复）。"""
    EXECUTION_TYPES[name] = ExecutionTypeSpec(
        name=name,
        payload_schema_version=1,
        payload_allowed_fields=frozenset(),
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        retry_backoff_base_seconds=base,
        retry_backoff_factor=factor,
    )
    EXECUTION_HANDLERS[name] = handler


def _unregister(name):
    EXECUTION_TYPES.pop(name, None)
    EXECUTION_HANDLERS.pop(name, None)


def _make_config(db_file):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


# ---- 1. 单 Worker 成功执行 system.ping ----


def test_worker_success_system_ping(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(
        _session_factory(db_session), worker_id="w1", clock=_clock()
    )
    stats = worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert stats["succeeded"] == 1
    attempt = (
        db_session.query(ExecutionAttempt)
        .filter(ExecutionAttempt.execution_id == execution.id)
        .one()
    )
    assert attempt.status == AttemptStatus.SUCCEEDED.value
    assert attempt.attempt_number == 1
    assert json.loads(attempt.result_text) == {"pong": True}
    # 幂等：已终态 execution 不再被 claim
    assert _claim(db_session) is None


# ---- 2. 两个竞争 claim 只有一个获胜 ----


def test_competing_claims_only_one_wins(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    engine = db_session.get_bind()
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    barrier = threading.Barrier(3)
    winners = []
    errors = []

    def _try_claim(worker_id):
        db = factory()
        try:
            barrier.wait(timeout=10)
            claimed = claim_next(
                db, worker_id, clock=_clock(), lease_ttl_seconds=60
            )
            if claimed is not None:
                winners.append(worker_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    threads = [
        threading.Thread(target=_try_claim, args=(f"worker-{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    barrier.wait(timeout=10)
    for t in threads:
        t.join(timeout=15)
    assert not errors, errors
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"


# ---- 3. attempt number 唯一（数据库约束） ----


def test_attempt_number_unique_constraint(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    claimed = _claim(db_session)
    assert claimed is not None
    _, attempt = claimed
    # 手工插入同 execution 同编号 → UNIQUE 约束拒绝
    dup = ExecutionAttempt(
        execution_id=execution.id,
        attempt_number=attempt.attempt_number,
        worker_id="other",
        lease_token="t" * 64,
        lease_expires_at=FIXED_NOW,
        status=AttemptStatus.CLAIMED.value,
        created_at=FIXED_NOW,
        updated_at=FIXED_NOW,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    # retry 只新增编号：第二次 claim 后编号递增
    clock = FakeClock()
    clock.advance(61)  # lease 过期
    recover_expired(db_session, clock=clock)
    claimed2 = claim_next(
        db_session, "w2", clock=clock, lease_ttl_seconds=60
    )
    assert claimed2 is not None
    _, attempt2 = claimed2
    assert attempt2.attempt_number == attempt.attempt_number + 1


# ---- 4. Worker claim 后崩溃：lease 过期回收 ----


def test_worker_crash_after_claim_recovered(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    clock = FakeClock()
    claimed = claim_next(db_session, "crashy", clock=clock, lease_ttl_seconds=60)
    assert claimed is not None
    _, attempt = claimed
    # 崩溃：不 finish，直接推进时钟过 lease
    clock.advance(61)
    recovered = recover_expired(db_session, clock=clock)
    assert recovered == 1
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.QUEUED.value
    assert execution.lease_owner is None  # 镜像租约已清（token 权威在 attempt）
    db_session.refresh(attempt)
    assert attempt.status == AttemptStatus.ABORTED.value  # 审计保留


# ---- 5. lease 未过期其他 Worker 不可接管 ----


def test_lease_not_expired_cannot_be_taken(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    clock = FakeClock()
    claimed = _claim(db_session, worker_id="w1", clock=clock)
    assert claimed is not None
    execution2, attempt = claimed
    # 心跳续期：lease 推到更远
    renew_lease(db_session, execution2, attempt, clock=clock, lease_ttl_seconds=60)
    # 其他 worker 不可接管（lease 未过期）
    clock.advance(30)
    assert claim_next(db_session, "w2", clock=clock, lease_ttl_seconds=60) is None


# ---- 6. lease 过期后安全回收 ----


def test_lease_expired_safe_recovery(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    clock = FakeClock()
    _claim(db_session, worker_id="w1", clock=clock)
    clock.advance(61)
    assert recover_expired(db_session, clock=clock) == 1
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.QUEUED.value
    claimed = claim_next(db_session, "w2", clock=clock, lease_ttl_seconds=60)
    assert claimed is not None
    assert claimed[1].attempt_number == 2  # 新 attempt，旧 attempt 保留


# ---- 7. 失败后按确定性 backoff 重试 ----


def test_failure_retry_with_backoff(db_session):
    _register(
        db_session, "test.flaky", max_attempts=3, base=2.0, factor=2.0,
        handler=lambda payload: (_ for _ in ()).throw(
            RuntimeError("boom with sk-super-secret-123")
        ),
    )
    try:
        clock = FakeClock()
        execution = create_execution(
            db_session,
            execution_type="test.flaky",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        enqueue(db_session, execution.id)
        worker = ExecutionWorker(
            _session_factory(db_session), worker_id="w1", clock=clock,
            lease_ttl_seconds=60,
        )
        stats = worker.run_once()
        assert stats["retried"] == 1
        db_session.refresh(execution)
        assert execution.status == ExecutionStatus.QUEUED.value
        # 确定性公式：delay = base * factor ** (attempt_number - 1)
        assert backoff_seconds(EXECUTION_TYPES["test.flaky"], 1) == 2.0
        assert backoff_seconds(EXECUTION_TYPES["test.flaky"], 2) == 4.0
        assert execution.retry_after == _naive(clock.now()) + timedelta(seconds=2.0)
        # retry_after 未到：不可领取
        clock.advance(1.5)
        assert claim_next(db_session, "w1", clock=clock) is None
        # 到点：可领取（attempt 2）
        clock.advance(0.5)
        claimed = claim_next(db_session, "w1", clock=clock)
        assert claimed is not None
        assert claimed[1].attempt_number == 2
    finally:
        _unregister("test.flaky")


# ---- 8. max_attempts 耗尽 → FAILED ----


def test_max_attempts_exhausted_failed(db_session):
    _register(
        db_session, "test.never", max_attempts=2, base=0.001, factor=2.0,
        handler=lambda payload: (_ for _ in ()).throw(RuntimeError("always fails")),
    )
    try:
        clock = FakeClock()
        execution = create_execution(
            db_session,
            execution_type="test.never",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        enqueue(db_session, execution.id)
        worker = ExecutionWorker(
            _session_factory(db_session), worker_id="w1", clock=clock,
            lease_ttl_seconds=60,
        )
        # 第一次：重试；第二次：FAILED
        stats1 = worker.run_once()
        assert stats1["retried"] == 1
        clock.advance(1)
        stats2 = worker.run_once()
        assert stats2["failed"] == 1
        db_session.refresh(execution)
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.last_error_code == "EXEC_HANDLER_ERROR"
        # 旧 attempt 全部保留为审计（绝不覆写）
        attempts = (
            db_session.query(ExecutionAttempt)
            .filter(ExecutionAttempt.execution_id == execution.id)
            .order_by(ExecutionAttempt.attempt_number)
            .all()
        )
        assert [a.attempt_number for a in attempts] == [1, 2]
        assert all(a.status == AttemptStatus.FAILED.value for a in attempts)
        assert execution.attempt_count == 2
        # 终态不再被领取
        assert claim_next(db_session, "w1", clock=clock) is None
    finally:
        _unregister("test.never")


# ---- 9. RUNNING 超时 → TIMED_OUT ----


def test_running_timeout_goes_timed_out(db_session):
    _register(
        db_session, "test.slow", timeout_seconds=10, handler=lambda p: {"ok": True}
    )
    try:
        clock = FakeClock()
        execution = create_execution(
            db_session,
            execution_type="test.slow",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        enqueue(db_session, execution.id)
        claimed = claim_next(db_session, "w1", clock=clock)
        assert claimed is not None
        transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
        clock.advance(11)
        timed_out = check_timeouts(db_session, clock=clock)
        assert timed_out == 1
        db_session.refresh(execution)
        assert execution.status == ExecutionStatus.TIMED_OUT.value
        assert execution.finished_at == _naive(clock.now())
        attempt = (
            db_session.query(ExecutionAttempt)
            .filter(ExecutionAttempt.execution_id == execution.id)
            .one()
        )
        assert attempt.status == AttemptStatus.ABORTED.value
    finally:
        _unregister("test.slow")


# ---- 10. CREATED/QUEUED 不得因等待进入 TIMED_OUT ----


def test_created_queued_never_timed_out(db_session):
    _register(
        db_session, "test.slow", timeout_seconds=10, handler=lambda p: {"ok": True}
    )
    try:
        clock = FakeClock()
        created = create_execution(
            db_session,
            execution_type="test.slow",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        queued = create_execution(
            db_session,
            execution_type="test.slow",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        enqueue(db_session, queued.id)
        clock.advance(999)
        assert check_timeouts(db_session, clock=clock) == 0
        db_session.refresh(created)
        db_session.refresh(queued)
        assert created.status == ExecutionStatus.CREATED.value
        assert queued.status == ExecutionStatus.QUEUED.value
    finally:
        _unregister("test.slow")


# ---- 11. CLAIMED lease 过期 → QUEUED（不是 TIMED_OUT） ----


def test_claimed_lease_expired_requeued_not_timed_out(db_session):
    _register(
        db_session, "test.slow", timeout_seconds=10, handler=lambda p: {"ok": True}
    )
    try:
        clock = FakeClock()
        execution = create_execution(
            db_session,
            execution_type="test.slow",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        enqueue(db_session, execution.id)
        _claim(db_session, worker_id="w1", clock=clock)
        clock.advance(61)  # lease 过期但从未开始执行
        assert recover_expired(db_session, clock=clock) == 1
        db_session.refresh(execution)
        assert execution.status == ExecutionStatus.QUEUED.value
        assert execution.started_at is None  # 未开始 → 不是 TIMED_OUT
    finally:
        _unregister("test.slow")


# ---- 12. QUEUED 取消 → CANCELLED ----


def test_queued_cancel_goes_cancelled(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    cancelled = request_cancel(db_session, execution.id)
    assert cancelled.status == ExecutionStatus.CANCELLED.value
    assert cancelled.finished_at is not None


# ---- 13. RUNNING 取消 → CANCEL_REQUESTED → Worker 确认 CANCELLED ----


def test_running_cancel_worker_confirms_cancelled(db_session):
    """claim → RUNNING → 执行前收到取消 → Worker 同一 tick 确认 CANCELLED。"""
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    clock = FakeClock()
    claimed = _claim(db_session, worker_id="w1", clock=clock)
    assert claimed is not None
    transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
    request_cancel(db_session, execution.id)  # RUNNING → CANCEL_REQUESTED
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.CANCEL_REQUESTED.value
    # Worker 在同一 tick 内继续处理该 execution：执行前检查 cancel → 确认取消
    worker = ExecutionWorker(
        _session_factory(db_session), worker_id="w1", clock=clock
    )
    outcome = worker._run_claimed(db_session, execution, claimed[1])
    assert outcome == "cancelled"
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.CANCELLED.value
    assert execution.finished_at is not None
    attempt = (
        db_session.query(ExecutionAttempt)
        .filter(ExecutionAttempt.execution_id == execution.id)
        .one()
    )
    assert attempt.status == AttemptStatus.ABORTED.value


# ---- 14. cancel 与 success race：单一终态（完成胜出） ----


def test_cancel_success_race_single_terminal(db_session):
    """handler 执行期间 cancel 到达 → Worker 完成后完成胜出
    （CANCEL_REQUESTED → SUCCEEDED）。与 test 13（Worker 先见取消 →
    CANCELLED）互为条件 UPDATE 裁决的两条路径，至多一个终态。"""
    execution = _make(db_session)
    enqueue(db_session, execution.id)

    def _cancel_during_execution(payload):
        # handler 执行中收到取消请求（模拟真实并发时序）
        request_cancel(db_session, execution.id, clock=clock)
        return {"done": True}

    _register(db_session, "test.race_cancel", handler=_cancel_during_execution)
    try:
        clock = FakeClock()
        worker = ExecutionWorker(
            _session_factory(db_session), worker_id="w1", clock=clock
        )
        stats = worker.run_once()
        assert stats["succeeded"] == 1
        db_session.refresh(execution)
        assert execution.status == ExecutionStatus.SUCCEEDED.value
        assert execution.finished_at is not None
        attempt = (
            db_session.query(ExecutionAttempt)
            .filter(ExecutionAttempt.execution_id == execution.id)
            .one()
        )
        assert attempt.status == AttemptStatus.SUCCEEDED.value
    finally:
        _unregister("test.race_cancel")


# ---- 15. timeout 与 success race：单一终态 ----


def test_timeout_success_race_single_terminal(db_session):
    _register(
        db_session, "test.slow", timeout_seconds=10, handler=lambda p: {"ok": True}
    )
    try:
        clock = FakeClock()
        execution = create_execution(
            db_session,
            execution_type="test.slow",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        enqueue(db_session, execution.id)
        claimed = claim_next(db_session, "w1", clock=clock)
        assert claimed is not None
        transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
        clock.advance(11)  # 超时已到
        # 顺序 A：sweeper 先裁决 → TIMED_OUT
        assert check_timeouts(db_session, clock=clock) == 1
        db_session.refresh(execution)
        assert execution.status == ExecutionStatus.TIMED_OUT.value
        # 顺序 B：worker 已完成，sweeper 后到 → 条件 UPDATE rowcount=0 不动
        done = create_execution(
            db_session,
            execution_type="test.slow",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        enqueue(db_session, done.id)
        claimed2 = claim_next(db_session, "w2", clock=clock)
        assert claimed2 is not None
        transition_to(db_session, done.id, ExecutionStatus.RUNNING, clock=clock)
        transition_to(db_session, done.id, ExecutionStatus.SUCCEEDED, clock=clock)
        assert check_timeouts(db_session, clock=clock) == 0
        db_session.refresh(done)
        assert done.status == ExecutionStatus.SUCCEEDED.value
    finally:
        _unregister("test.slow")


# ---- 16. 终态 execution 不被重新 claim ----


def test_terminal_execution_never_reclaimed(db_session):
    clock = FakeClock()
    # 四个终态各造一个（各走合法路径）
    for target in (
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.TIMED_OUT,
    ):
        ex = _make(db_session)
        _walk(db_session, ex, ExecutionStatus.QUEUED, ExecutionStatus.CLAIMED,
              ExecutionStatus.RUNNING, target)
    cancelled = _make(db_session)
    _walk(db_session, cancelled, ExecutionStatus.QUEUED, ExecutionStatus.CANCELLED)
    assert claim_next(db_session, "w1", clock=clock) is None


# ---- 17. Worker 重启后的恢复 ----


def test_worker_restart_recovery(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    clock = FakeClock()
    # worker A 领取后崩溃（不 finish）
    _claim(db_session, worker_id="worker-a", clock=clock)
    clock.advance(61)  # 租约过期
    # worker B 重启：recover → 重新领取 → 成功执行
    worker_b = ExecutionWorker(
        _session_factory(db_session), worker_id="worker-b", clock=clock,
        lease_ttl_seconds=60,
    )
    stats = worker_b.run_once()
    assert stats["recovered"] == 1
    assert stats["succeeded"] == 1
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert execution.attempt_count == 2
    attempts = (
        db_session.query(ExecutionAttempt)
        .filter(ExecutionAttempt.execution_id == execution.id)
        .order_by(ExecutionAttempt.attempt_number)
        .all()
    )
    assert attempts[0].status == AttemptStatus.ABORTED.value  # worker A 崩溃痕迹
    assert attempts[0].worker_id == "worker-a"
    assert attempts[1].status == AttemptStatus.SUCCEEDED.value
    assert attempts[1].worker_id == "worker-b"


# ---- 18. result / error sanitization ----


def test_result_and_error_sanitization(db_session, monkeypatch):
    monkeypatch.setattr(settings, "llm_api_key", "sk-super-secret-123")
    # sanitize 模块的替换清单在导入时固化——测试同步替换（生产 settings 不变）
    monkeypatch.setattr(
        "app.core.sanitize._SECRET_VALUES",
        (settings.database_url, "sk-super-secret-123", settings.embedding_api_key),
    )

    def _leaky_result(payload):
        return {
            "api_key": "sk-leaked-999",
            "auth_token": "tok-abc",
            "long_text": "x" * 2000,
            "ok": True,
        }

    _register(
        db_session, "test.leaky", handler=_leaky_result
    )
    try:
        clock = FakeClock()
        execution = create_execution(
            db_session,
            execution_type="test.leaky",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        enqueue(db_session, execution.id)
        worker = ExecutionWorker(
            _session_factory(db_session), worker_id="w1", clock=clock
        )
        worker.run_once()
        attempt = (
            db_session.query(ExecutionAttempt)
            .filter(ExecutionAttempt.execution_id == execution.id)
            .one()
        )
        result = json.loads(attempt.result_text)
        # 敏感字段整字段替换（值一并脱敏）
        assert result["<redacted>"] == "<redacted>"
        assert "sk-leaked-999" not in attempt.result_text
        assert "tok-abc" not in attempt.result_text
        # 长文本截断
        assert len(result["long_text"]) == 500
        assert result["ok"] is True
    finally:
        _unregister("test.leaky")

    # 错误消息脱敏：handler 抛错含配置密钥 → 落盘前替换
    def _explode(payload):
        raise RuntimeError(f"db connect failed using {settings.llm_api_key}")

    _register(db_session, "test.explode", max_attempts=1, handler=_explode)
    try:
        clock = FakeClock()
        execution = create_execution(
            db_session,
            execution_type="test.explode",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
        )
        enqueue(db_session, execution.id)
        worker = ExecutionWorker(
            _session_factory(db_session), worker_id="w1", clock=clock
        )
        worker.run_once()
        attempt = (
            db_session.query(ExecutionAttempt)
            .filter(ExecutionAttempt.execution_id == execution.id)
            .one()
        )
        assert "sk-super-secret-123" not in attempt.error_message
        assert "<redacted>" in attempt.error_message
        assert len(attempt.error_message) <= 500
    finally:
        _unregister("test.explode")


# ---- 19. SQLite 单 Worker 限制明确生效 ----


def test_sqlite_single_worker_enforced(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    clock = FakeClock()
    factory = _session_factory(db_session)
    worker_a = ExecutionWorker(
        factory, worker_id="worker-a", clock=clock, lease_ttl_seconds=60
    )
    assert worker_a.run_once()["succeeded"] == 1  # 正常执行
    # 另一活跃租约存在（模拟另一个 worker 正在处理）→ 拒绝启动
    busy = _make(db_session)
    enqueue(db_session, busy.id)
    claim_next(db_session, "worker-x", clock=clock, lease_ttl_seconds=60)
    with pytest.raises(ExecutionWorkerConflictError):
        ExecutionWorker(factory, worker_id="worker-b", clock=clock)


# ---- 20. PG claim SQL 离线可生成 + PG migration SQL ----


def test_pg_claim_sql_offline_generatable(db_session):
    from sqlalchemy.dialects import postgresql

    now = FIXED_NOW
    sql = str(
        update(Execution)
        .where(
            Execution.status == ExecutionStatus.QUEUED.value,
            Execution.run_at.is_(None),
        )
        .values(status=ExecutionStatus.CLAIMED.value, lease_expires_at=now)
        .compile(dialect=postgresql.dialect())
    )
    assert "UPDATE executions" in sql
    assert "SET status" in sql
    assert "WHERE executions.status" in sql
    assert "executions.run_at" in sql
    # claim 的 CAS 形状：单语句条件 UPDATE（与 SELECT ... FOR UPDATE
    # SKIP LOCKED 等价的单语句原子方案）
    assert "IS NULL" in sql


def test_migration_sqlite_roundtrip_6b(tmp_path):
    db_file = tmp_path / "migrate6b.db"
    cfg = _make_config(db_file)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "execution_attempts" in tables
    assert "executions" in tables
    columns = {col["name"] for col in inspector.get_columns("execution_attempts")}
    assert {
        "execution_id", "attempt_number", "worker_id", "lease_token",
        "lease_expires_at", "status", "result_text",
    } <= columns
    indexes = {
        idx["name"]: idx["unique"] for idx in inspector.get_indexes("execution_attempts")
    }
    assert indexes.get("uq_execution_attempts_number")  # 编号唯一约束
    fks = inspector.get_foreign_keys("execution_attempts")
    assert any(fk["referred_table"] == "executions" for fk in fks)
    assert any(
        fk["referred_table"] == "executions"
        and "CASCADE" in str(fk.get("options", {}))
        for fk in fks
    )
    engine.dispose()
    # downgrade：只移除 6B 新增对象，既有表保留
    command.downgrade(cfg, "6a7b8c9d0e1f2")
    engine = create_engine(f"sqlite:///{db_file}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "execution_attempts" not in tables
    assert "executions" in tables
    assert "tasks" in tables
    engine.dispose()
    # re-upgrade 恢复
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    assert "execution_attempts" in set(inspect(engine).get_table_names())
    con = sqlite3.connect(db_file)
    assert (
        con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == "8c9d0e1f2a3b4"
    )
    con.close()
    engine.dispose()


def test_pg_offline_sql_generation_6b(capsys):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:pass@localhost:5432/jarvis_pg_check",
    )
    command.upgrade(cfg, "head", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE execution_attempts" in sql
    assert "CREATE UNIQUE INDEX uq_execution_attempts_number" in sql
    assert "ON DELETE CASCADE" in sql


# ---- 迁移链：单 head 与 revision 链 ----


def test_single_head_and_chain():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    heads = set(ScriptDirectory.from_config(cfg).get_heads())
    assert heads == {"8c9d0e1f2a3b4"}
    # 6C revision 的直接 down_revision 是 6B
    import importlib.util

    versions = Path(BACKEND_DIR / "alembic" / "versions")
    mods = {}
    for path in versions.glob("*.py"):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods[mod.revision] = mod.down_revision
    assert mods["8c9d0e1f2a3b4"] == "7b8c9d0e1f2a3"
    assert mods["7b8c9d0e1f2a3"] == "6a7b8c9d0e1f2"
    assert mods["6a7b8c9d0e1f2"] == "e6f5d4c3b2a1"


# ---- 租约丢失：旧 Worker 的收尾必须失效 ----


def test_stale_worker_finish_fails_after_requeue(db_session):
    """recover 回收后，旧 worker 的 finish 判定租约丢失（EXEC_CLAIM_LOST）。"""
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    clock = FakeClock()
    claimed = _claim(db_session, worker_id="crashy", clock=clock)
    assert claimed is not None
    transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
    clock.advance(61)
    recover_expired(db_session, clock=clock)
    # 旧 worker 尝试收尾（模拟崩溃后幽灵进程）→ 条件 UPDATE 不匹配 → 抛
    ghost = ExecutionWorker(
        _session_factory(db_session), worker_id="crashy", clock=clock,
        lease_ttl_seconds=60,
    )
    with pytest.raises(ExecutionClaimLostError):
        ghost._finish_attempt(
            db_session, claimed[1], AttemptStatus.SUCCEEDED, clock.now(),
            result_text="{}",
        )
    db_session.refresh(execution)
    # 状态保持 QUEUED（可被新 worker 领取），绝无双终态
    assert execution.status == ExecutionStatus.QUEUED.value
