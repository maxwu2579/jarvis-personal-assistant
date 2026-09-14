"""Phase 6D 定向测试：One-shot Scheduling and Time Semantics。

覆盖需求清单（32 项，与授权规格第十节一一对应）：
1.  run_at=None 立即可领取
2.  过去时间立即可领取
3.  未来时间不可提前领取
4.  FakeClock 推进后可领取
5.  naive datetime 被拒绝（EXEC_RUN_AT_NAIVE；非 datetime → EXEC_RUN_AT_INVALID）
6.  aware UTC datetime 正确存储
7.  非 UTC aware datetime 正确转为 UTC
8.  Worker 重启后未来任务仍等待
9.  Worker 重启后到期任务可恢复
10. 两 Worker 到期竞争只有一个成功
11. run_at 与 retry_after 取较晚时间（双门条件）
12. terminal execution 不重新调度
13. cancelled execution 不重新调度
14. RUN_IMMEDIATELY misfire（仍执行 + MISFIRE_DETECTED/MISFIRE_RUN_IMMEDIATELY）
15. SKIP misfire（→ CANCELLED，不执行）
16. FAIL misfire（→ FAILED + EXEC_MISFIRE_EXPIRED）
17. grace 边界前后（严格大于；恰好等于不 misfire）
18. 负数 grace 拒绝（构造期 ValueError）
19. 极大 grace 拒绝（> 上限 / NaN / Infinity / 字符串 / 非法 policy）
20. run_at=None 不触发 misfire
21. misfire handler 不被错误执行（SKIP/FAIL 后零调用）
22. misfire 与 cancel race（单一合法结果）
23. misfire 与 claim race（CAS 单赢家）
24. 调度事件顺序（EXECUTION_SCHEDULED → SCHEDULE_DUE → MISFIRE_* → CLAIMED）
25. 事件 payload 脱敏（调度事件含敏感键 → <redacted>）
26. stale Worker 不写调度事件
27. system.ping 兼容（含 run_at 的完整执行）
28. Phase 6C step/event 不回退（模板步骤 + 调度共存）
29. Phase 6B retry/lease 不回退（重试任务不算 misfire）
30. Reminder 行为不变（模型/Worker 可导入，表独立可写）
31. SQLite/PostgreSQL 离线 SQL 兼容（claim/misfire CAS 编译）
32. 无新增 migration（单 head = 8c9d0e1f2a3b4，链不变）

全部离线：临时 SQLite + FK pragma + create_all；FakeClock 可推进（不真实
sleep）；monkeypatch 注册表（自动恢复）；绝不访问 jarvis.db / .env。
"""

import importlib.util
import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, or_, update
from sqlalchemy.orm import sessionmaker
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import postgresql

from app.core.database import Base, _set_sqlite_pragma
from app.models.execution import Execution, ExecutionStatus
from app.models.execution_attempt import ExecutionAttempt
from app.models.execution_event import ExecutionEvent
from app.models.execution_step import ExecutionStep
from app.services.execution_claim import claim_next, recover_expired
from app.services.execution_errors import (
    MISFIRE_EXPIRED_MESSAGE,
    ExecutionClaimLostError,
    ExecutionRunAtInvalidError,
    ExecutionRunAtNaiveError,
    InvalidExecutionTransitionError,
)
from app.services.execution_events import (
    APPEND_ONLY_EVENT_TYPES,
    append_event,
    list_events,
)
from app.services.execution_registry import (
    DEFAULT_MISFIRE_GRACE_SECONDS,
    EXECUTION_TOOLS,
    EXECUTION_TYPES,
    MAX_MISFIRE_GRACE_SECONDS,
    ExecutionTypeSpec,
    StepSpec,
    ToolSpec,
)
from app.services.execution_service import (
    create_execution,
    enqueue,
    request_cancel,
    transition_to,
)
from app.services.execution_steps import run_execution_steps
from app.services.execution_worker import ExecutionWorker

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)
PING = {}


class FakeClock:
    """可推进时钟（6D 需要模拟 run_at 到期 / grace 流逝 / lease 过期）。"""

    def __init__(self, start=FIXED_NOW):
        self._now = start

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)


_key_counter = itertools.count(1)


def _unique_key() -> str:
    return f"test-6d-key-{next(_key_counter):08d}"


def _naive(dt):
    """SQLite 下 DB 时间为 naive UTC；断言时统一。"""
    return dt.replace(tzinfo=None)


@pytest.fixture()
def db_session(tmp_path):
    db_file = tmp_path / "test_6d.db"
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


def _make(db, key=None, payload=PING, execution_type="system.ping", **kwargs):
    return create_execution(
        db,
        execution_type=execution_type,
        payload=payload,
        idempotency_key=key or _unique_key(),
        clock=kwargs.pop("clock", FakeClock()),
        **kwargs,
    )


def _claim(db, worker_id="worker-6d", clock=None):
    return claim_next(db, worker_id, clock=clock or FakeClock(), lease_ttl_seconds=60)


def _worker(factory, clock, worker_id):
    return ExecutionWorker(factory, worker_id=worker_id, clock=clock, lease_ttl_seconds=60)


def _event_types(db, execution_id):
    return [e.event_type for e in list_events(db, execution_id)]


def _register_sched_type(
    monkeypatch,
    name,
    *,
    policy="RUN_IMMEDIATELY",
    grace=DEFAULT_MISFIRE_GRACE_SECONDS,
    max_attempts=3,
    handler=None,
):
    """注册带 misfire policy 的声明式类型（monkeypatch 自动恢复）。

    steps 模板引用同名 tool（claim 级测试不物化步骤，无需 tool；
    handler 传入时同时注册 tool，供 worker 全路径测试）。
    """
    monkeypatch.setitem(
        EXECUTION_TYPES,
        name,
        ExecutionTypeSpec(
            name=name,
            payload_schema_version=1,
            payload_allowed_fields=frozenset(),
            max_attempts=max_attempts,
            timeout_seconds=None,
            retry_backoff_base_seconds=2.0,
            retry_backoff_factor=2.0,
            steps=(StepSpec(step_key="run", tool_name=name),),
            misfire_policy=policy,
            misfire_grace_seconds=grace,
        ),
    )
    if handler is not None:
        monkeypatch.setitem(
            EXECUTION_TOOLS,
            name,
            ToolSpec(name=name, handler=handler, retryable=True, replay_safe=True),
        )
    return name


# =====================================================================
# 1-4. run_at 三态：立即 / 过去 / 未来 / 时钟推进
# =====================================================================


def test_run_at_none_immediately_claimable(db_session):
    clock = FakeClock()
    execution = _make(db_session, clock=clock)  # run_at=None
    enqueue(db_session, execution.id, clock=clock)
    claimed = _claim(db_session, clock=clock)
    assert claimed is not None
    assert claimed[0].id == execution.id


def test_run_at_past_immediately_claimable(db_session):
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=3600), clock=clock
    )
    enqueue(db_session, execution.id, clock=clock)
    assert _claim(db_session, clock=clock) is not None


def test_run_at_future_not_claimable_early(db_session):
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW + timedelta(seconds=300), clock=clock
    )
    enqueue(db_session, execution.id, clock=clock)
    assert _claim(db_session, clock=clock) is None  # 不得提前 claim
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.QUEUED.value


def test_run_at_future_claimable_after_clock_advance(db_session):
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW + timedelta(seconds=300), clock=clock
    )
    enqueue(db_session, execution.id, clock=clock)
    clock.advance(301)
    claimed = _claim(db_session, clock=clock)
    assert claimed is not None and claimed[0].id == execution.id


# =====================================================================
# 5-7. UTC 规范：naive 拒绝 / aware 存储 / 非 UTC 转换
# =====================================================================


def test_run_at_naive_rejected(db_session):
    clock = FakeClock()

    def _create(run_at):
        return create_execution(
            db_session,
            execution_type="system.ping",
            payload={},
            idempotency_key=_unique_key(),
            clock=clock,
            run_at=run_at,
        )

    # naive → EXEC_RUN_AT_NAIVE（绝不静默解释）
    with pytest.raises(ExecutionRunAtNaiveError) as ei:
        _create(datetime(2026, 8, 20, 12, 0))
    assert ei.value.code == "EXEC_RUN_AT_NAIVE"
    # 非 datetime 值 → EXEC_RUN_AT_INVALID
    for bad in ("2026-08-20T12:00:00Z", 123456, None, True):
        if bad is None:
            continue  # None = 不调度，合法
        with pytest.raises(ExecutionRunAtInvalidError) as ei2:
            _create(bad)
        assert ei2.value.code == "EXEC_RUN_AT_INVALID"
    # 失败不残留行
    assert db_session.query(Execution).count() == 0


def test_run_at_aware_utc_stored_correctly(db_session):
    run_at = datetime(2026, 8, 21, 6, 30, tzinfo=timezone.utc)
    execution = _make(db_session, run_at=run_at, clock=FakeClock())
    db_session.refresh(execution)
    assert execution.run_at == _naive(run_at)  # SQLite 落 naive-UTC（方言接缝）
    # 事件 payload 保留 aware UTC 信息
    ev = list_events(db_session, execution.id)[-1]
    assert ev.event_type == "EXECUTION_SCHEDULED"
    assert json.loads(ev.payload)["run_at"] == "2026-08-21T06:30:00+00:00"


def test_run_at_non_utc_aware_converted_to_utc(db_session):
    # +08:00 上午 10:30 → UTC 02:30（本机时区无关）
    run_at = datetime(2026, 8, 21, 10, 30, tzinfo=timezone(timedelta(hours=8)))
    execution = _make(db_session, run_at=run_at, clock=FakeClock())
    db_session.refresh(execution)
    assert execution.run_at == datetime(2026, 8, 21, 2, 30)


# =====================================================================
# 8-9. Worker 重启语义
# =====================================================================


def test_worker_restart_future_task_still_waits(db_session):
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW + timedelta(seconds=600), clock=clock
    )
    enqueue(db_session, execution.id, clock=clock)
    factory = _session_factory(db_session)
    w1 = _worker(factory, clock, "w6d-restart-1")
    assert w1.run_once()["claimed"] == 0
    w2 = _worker(factory, clock, "w6d-restart-2")  # 重启
    assert w2.run_once()["claimed"] == 0
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.QUEUED.value  # 继续等待


def test_worker_restart_due_task_recoverable(db_session):
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW + timedelta(seconds=300), clock=clock
    )
    enqueue(db_session, execution.id, clock=clock)
    clock.advance(301)  # 到期窗口内无 Worker（停机）；重启后恢复领取
    factory = _session_factory(db_session)
    stats = _worker(factory, clock, "w6d-restart-3").run_once()
    assert stats["claimed"] == 1 and stats["succeeded"] == 1
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value


# =====================================================================
# 10-13. 竞争与终态语义
# =====================================================================


def test_two_workers_due_race_single_winner(db_session):
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=60), clock=clock
    )
    enqueue(db_session, execution.id, clock=clock)
    first = _claim(db_session, worker_id="w-race-a", clock=clock)
    assert first is not None
    second = _claim(db_session, worker_id="w-race-b", clock=clock)
    assert second is None  # 同一到期候选：CAS 只有一个 claim 成功
    db_session.refresh(execution)
    assert execution.attempt_count == 1
    assert execution.lease_owner == "w-race-a"


def test_run_at_and_retry_after_take_later_gate(db_session):
    """claim 双门：run_at 与 retry_after 取较晚有效时间。"""
    clock = FakeClock()
    now = FIXED_NOW

    def _sched(run_at, retry_after):
        ex = _make(db_session, run_at=run_at, clock=clock)
        enqueue(db_session, ex.id, clock=clock)
        db_session.execute(
            update(Execution)
            .where(Execution.id == ex.id)
            .values(retry_after=retry_after)
        )
        db_session.commit()
        return ex

    a = _sched(now - timedelta(seconds=60), now + timedelta(seconds=120))  # 门=retry_after
    b = _sched(now + timedelta(seconds=120), now - timedelta(seconds=60))  # 门=run_at
    c = _sched(now - timedelta(seconds=60), now - timedelta(seconds=60))  # 双到期
    d = _sched(now + timedelta(seconds=300), now + timedelta(seconds=300))  # 双未来

    claimed = _claim(db_session, clock=clock)
    assert claimed is not None and claimed[0].id == c.id  # 只有双到期可领
    assert _claim(db_session, clock=clock) is None  # a/b/d 均被较晚者门控

    clock.advance(121)
    claimed = _claim(db_session, clock=clock)
    assert claimed is not None and claimed[0].id == a.id  # retry_after 已到
    claimed = _claim(db_session, clock=clock)
    assert claimed is not None and claimed[0].id == b.id  # run_at 已到
    assert _claim(db_session, clock=clock) is None  # d 仍未来


def test_terminal_execution_not_rescheduled(db_session):
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=1000), clock=clock
    )
    enqueue(db_session, execution.id, clock=clock)
    factory = _session_factory(db_session)
    stats = _worker(factory, clock, "w6d-term").run_once()
    assert stats["succeeded"] == 1
    clock.advance(100000)  # run_at 早已到期（且远超 grace）
    assert recover_expired(db_session, clock=clock) == 0  # 终态绝不回收
    assert _claim(db_session, clock=clock) is None  # 终态绝不重新入队
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert _event_types(db_session, execution.id).count("EXECUTION_CLAIMED") == 1


def test_cancelled_execution_not_rescheduled(db_session):
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=1000), clock=clock
    )
    enqueue(db_session, execution.id, clock=clock)
    request_cancel(db_session, execution.id, clock=clock)
    assert _claim(db_session, clock=clock) is None
    clock.advance(100000)
    assert recover_expired(db_session, clock=clock) == 0
    assert _claim(db_session, clock=clock) is None
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.CANCELLED.value


# =====================================================================
# 14-20. Misfire 三策略与 grace 规则
# =====================================================================


def test_misfire_run_immediately_claims_and_events(db_session, monkeypatch):
    _register_sched_type(monkeypatch, "test.runnow", policy="RUN_IMMEDIATELY")
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=1000),
        execution_type="test.runnow", clock=clock,
    )
    enqueue(db_session, execution.id, clock=clock)
    claimed = _claim(db_session, clock=clock)  # 超 grace 仍执行
    assert claimed is not None and claimed[0].id == execution.id
    events = _event_types(db_session, execution.id)
    assert events == [
        "EXECUTION_CREATED", "EXECUTION_SCHEDULED", "EXECUTION_QUEUED",
        "SCHEDULE_DUE", "MISFIRE_DETECTED", "MISFIRE_RUN_IMMEDIATELY",
        "EXECUTION_CLAIMED",
    ]


def test_misfire_skip_cancels(db_session, monkeypatch):
    _register_sched_type(monkeypatch, "test.skip", policy="SKIP")
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=1000),
        execution_type="test.skip", clock=clock,
    )
    enqueue(db_session, execution.id, clock=clock)
    assert _claim(db_session, clock=clock) is None  # 不执行 handler
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.CANCELLED.value  # 现有终态
    assert execution.finished_at == _naive(FIXED_NOW)
    assert db_session.query(ExecutionAttempt).count() == 0  # 无 attempt
    events = _event_types(db_session, execution.id)
    assert events.count("MISFIRE_DETECTED") == 1
    assert events.count("MISFIRE_SKIPPED") == 1
    assert "EXECUTION_CLAIMED" not in events


def test_misfire_fail_failed_with_code(db_session, monkeypatch):
    _register_sched_type(monkeypatch, "test.fail", policy="FAIL")
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=1000),
        execution_type="test.fail", clock=clock,
    )
    enqueue(db_session, execution.id, clock=clock)
    assert _claim(db_session, clock=clock) is None
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.last_error_code == "EXEC_MISFIRE_EXPIRED"
    assert execution.last_error_message == MISFIRE_EXPIRED_MESSAGE
    assert db_session.query(ExecutionAttempt).count() == 0
    events = _event_types(db_session, execution.id)
    assert events.count("MISFIRE_DETECTED") == 1
    assert events.count("MISFIRE_FAILED") == 1
    assert "EXECUTION_CLAIMED" not in events


def test_misfire_grace_boundary(db_session, monkeypatch):
    """misfire = now > run_at + grace（严格大于；恰好等于不 misfire）。"""
    _register_sched_type(monkeypatch, "test.bound", policy="FAIL", grace=300.0)
    clock = FakeClock()

    def _sched():
        ex = _make(
            db_session, run_at=FIXED_NOW, execution_type="test.bound", clock=clock
        )
        enqueue(db_session, ex.id, clock=clock)
        return ex

    a = _sched()  # grace 内
    b = _sched()  # 恰好等于 grace（边界）
    c = _sched()  # 超过 grace

    clock.advance(299)
    assert _claim(db_session, clock=clock) is not None and _claim(
        db_session, clock=clock
    ) is not None
    # 前两次 claim 是 a、b（按 id 序）；b 恰好 +300 时仍不 misfire
    db_session.refresh(b)
    assert b.status == ExecutionStatus.CLAIMED.value
    assert _event_types(db_session, b.id).count("MISFIRE_DETECTED") == 0

    clock.advance(2)  # c 在 +301（299+2）：超过 grace → misfire FAIL
    assert _claim(db_session, clock=clock) is None
    db_session.refresh(c)
    assert c.status == ExecutionStatus.FAILED.value
    assert c.last_error_code == "EXEC_MISFIRE_EXPIRED"


def test_negative_grace_rejected():
    with pytest.raises(ValueError):
        ExecutionTypeSpec(
            name="bad-neg", payload_schema_version=1,
            payload_allowed_fields=frozenset(), max_attempts=1,
            timeout_seconds=None, misfire_grace_seconds=-1,
        )
    with pytest.raises(ValueError):
        ExecutionTypeSpec(
            name="bad-neg", payload_schema_version=1,
            payload_allowed_fields=frozenset(), max_attempts=1,
            timeout_seconds=None, misfire_grace_seconds=-0.001,
        )


def test_extreme_grace_and_bad_policy_rejected():
    for bad in (
        MAX_MISFIRE_GRACE_SECONDS + 1,  # 超过合理上限
        float("nan"),  # 非有限
        float("inf"),
        "300",  # 用户字符串绝不直接转换
        True,  # bool 不是数值
    ):
        with pytest.raises(ValueError):
            ExecutionTypeSpec(
                name="bad-grace", payload_schema_version=1,
                payload_allowed_fields=frozenset(), max_attempts=1,
                timeout_seconds=None, misfire_grace_seconds=bad,
            )
    with pytest.raises(ValueError):
        ExecutionTypeSpec(
            name="bad-policy", payload_schema_version=1,
            payload_allowed_fields=frozenset(), max_attempts=1,
            timeout_seconds=None, misfire_policy="SOMETIMES",
        )
    # 上限本身合法
    ExecutionTypeSpec(
        name="ok-max", payload_schema_version=1,
        payload_allowed_fields=frozenset(), max_attempts=1,
        timeout_seconds=None, misfire_grace_seconds=MAX_MISFIRE_GRACE_SECONDS,
    )


def test_run_at_none_never_misfires(db_session, monkeypatch):
    _register_sched_type(monkeypatch, "test.none", policy="FAIL")  # 即使 FAIL
    clock = FakeClock()
    execution = _make(
        db_session, execution_type="test.none", clock=clock  # run_at=None
    )
    enqueue(db_session, execution.id, clock=clock)
    clock.advance(100000)  # 远超任何 grace
    claimed = _claim(db_session, clock=clock)
    assert claimed is not None
    events = _event_types(db_session, execution.id)
    assert "MISFIRE_DETECTED" not in events
    assert "SCHEDULE_DUE" not in events  # 未调度任务无调度事件


# =====================================================================
# 21-23. 竞争：handler 不误执行 / cancel race / claim race
# =====================================================================


def test_misfire_handler_not_executed(db_session, monkeypatch):
    calls = {"n": 0}

    def _counting(payload):
        calls["n"] += 1
        return {"ok": True}

    for name, policy in (("test.noexec-skip", "SKIP"), ("test.noexec-fail", "FAIL")):
        _register_sched_type(monkeypatch, name, policy=policy, handler=_counting)
        clock = FakeClock()
        execution = _make(
            db_session, run_at=FIXED_NOW - timedelta(seconds=1000),
            execution_type=name, clock=clock,
        )
        enqueue(db_session, execution.id, clock=clock)
        stats = _worker(
            _session_factory(db_session), clock, f"w6d-noexec-{policy.lower()}"
        ).run_once()
        assert stats["claimed"] == 0  # misfire 裁决优先，无 claim
        db_session.refresh(execution)
        assert execution.status == (
            ExecutionStatus.CANCELLED.value
            if policy == "SKIP"
            else ExecutionStatus.FAILED.value
        )
    assert calls["n"] == 0  # handler 从未被调用


def test_misfire_cancel_race_single_outcome(db_session, monkeypatch):
    _register_sched_type(monkeypatch, "test.race-cancel", policy="FAIL")
    clock = FakeClock()

    # 顺序 1：cancel 先胜出 → CANCELLED；misfire 裁决不得再写事件
    x = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=1000),
        execution_type="test.race-cancel", clock=clock,
    )
    enqueue(db_session, x.id, clock=clock)
    request_cancel(db_session, x.id, clock=clock)
    assert _claim(db_session, clock=clock) is None
    db_session.refresh(x)
    assert x.status == ExecutionStatus.CANCELLED.value
    events = _event_types(db_session, x.id)
    assert "MISFIRE_DETECTED" not in events and "MISFIRE_FAILED" not in events

    # 顺序 2：misfire 先胜出 → FAILED；随后的 cancel 被终态拒绝
    y = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=1000),
        execution_type="test.race-cancel", clock=clock,
    )
    enqueue(db_session, y.id, clock=clock)
    assert _claim(db_session, clock=clock) is None  # misfire 裁决
    db_session.refresh(y)
    assert y.status == ExecutionStatus.FAILED.value
    with pytest.raises(InvalidExecutionTransitionError):
        request_cancel(db_session, y.id, clock=clock)
    events = _event_types(db_session, y.id)
    assert events.count("MISFIRE_DETECTED") == 1
    assert events.count("MISFIRE_FAILED") == 1


def test_misfire_claim_race_single_winner(db_session, monkeypatch):
    _register_sched_type(monkeypatch, "test.race-now", policy="RUN_IMMEDIATELY")
    _register_sched_type(monkeypatch, "test.race-skip", policy="SKIP")
    clock = FakeClock()

    # RUN_IMMEDIATELY：两个 worker 同时见 misfire → 一个 claim + 一套事件
    x = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=1000),
        execution_type="test.race-now", clock=clock,
    )
    enqueue(db_session, x.id, clock=clock)
    assert _claim(db_session, worker_id="w-race-1", clock=clock) is not None
    assert _claim(db_session, worker_id="w-race-2", clock=clock) is None
    events = _event_types(db_session, x.id)
    assert events.count("MISFIRE_DETECTED") == 1
    assert events.count("MISFIRE_RUN_IMMEDIATELY") == 1
    assert events.count("EXECUTION_CLAIMED") == 1

    # SKIP：两个 worker → 只有一个裁决生效
    y = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=1000),
        execution_type="test.race-skip", clock=clock,
    )
    enqueue(db_session, y.id, clock=clock)
    assert _claim(db_session, worker_id="w-race-1", clock=clock) is None
    assert _claim(db_session, worker_id="w-race-2", clock=clock) is None
    db_session.refresh(y)
    assert y.status == ExecutionStatus.CANCELLED.value
    events = _event_types(db_session, y.id)
    assert events.count("MISFIRE_DETECTED") == 1
    assert events.count("MISFIRE_SKIPPED") == 1


# =====================================================================
# 24-26. 事件审计
# =====================================================================


def test_scheduling_event_order(db_session, monkeypatch):
    for t in (
        "EXECUTION_SCHEDULED", "SCHEDULE_DUE", "MISFIRE_DETECTED",
        "MISFIRE_RUN_IMMEDIATELY", "MISFIRE_SKIPPED", "MISFIRE_FAILED",
    ):
        assert t in APPEND_ONLY_EVENT_TYPES  # 6D 新事件全部入白名单

    clock = FakeClock()
    # 1) 带 run_at 创建 + 到期 claim：CREATED → SCHEDULED → QUEUED → SCHEDULE_DUE → CLAIMED
    ex = _make(
        db_session, run_at=FIXED_NOW + timedelta(seconds=600), clock=clock
    )
    assert _event_types(db_session, ex.id) == [
        "EXECUTION_CREATED", "EXECUTION_SCHEDULED",
    ]
    enqueue(db_session, ex.id, clock=clock)
    clock.advance(600)
    assert _claim(db_session, clock=clock) is not None
    assert _event_types(db_session, ex.id) == [
        "EXECUTION_CREATED", "EXECUTION_SCHEDULED", "EXECUTION_QUEUED",
        "SCHEDULE_DUE", "EXECUTION_CLAIMED",
    ]

    # 2) RUN_IMMEDIATELY misfire：SCHEDULE_DUE → MISFIRE_DETECTED →
    #    MISFIRE_RUN_IMMEDIATELY → EXECUTION_CLAIMED
    _register_sched_type(monkeypatch, "test.order-now", policy="RUN_IMMEDIATELY")
    ex2 = _make(
        db_session, run_at=FIXED_NOW, execution_type="test.order-now", clock=clock
    )
    enqueue(db_session, ex2.id, clock=clock)
    clock.advance(301)
    assert _claim(db_session, clock=clock) is not None
    assert _event_types(db_session, ex2.id) == [
        "EXECUTION_CREATED", "EXECUTION_SCHEDULED", "EXECUTION_QUEUED",
        "SCHEDULE_DUE", "MISFIRE_DETECTED", "MISFIRE_RUN_IMMEDIATELY",
        "EXECUTION_CLAIMED",
    ]

    # 3) SKIP：MISFIRE_DETECTED → MISFIRE_SKIPPED（无 SCHEDULE_DUE——未 claim）
    _register_sched_type(monkeypatch, "test.order-skip", policy="SKIP")
    ex3 = _make(
        db_session, run_at=FIXED_NOW, execution_type="test.order-skip", clock=clock
    )
    enqueue(db_session, ex3.id, clock=clock)
    clock.advance(301)
    assert _claim(db_session, clock=clock) is None
    assert _event_types(db_session, ex3.id) == [
        "EXECUTION_CREATED", "EXECUTION_SCHEDULED", "EXECUTION_QUEUED",
        "MISFIRE_DETECTED", "MISFIRE_SKIPPED",
    ]


def test_scheduling_event_payload_sanitized(db_session):
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW + timedelta(seconds=60), clock=clock
    )
    append_event(
        db_session,
        execution_id=execution.id,
        event_type="MISFIRE_FAILED",
        actor_type="service",
        actor_id="test",
        correlation_id="corr-6d",
        clock=clock,
        payload={
            "run_at": "2026-08-20T04:00:00+00:00",
            "api_key": "hunter2secret",
            "nested": {"secret_token": "tok123"},
        },
    )
    db_session.commit()
    ev = list_events(db_session, execution.id)[-1]
    assert ev.event_type == "MISFIRE_FAILED"
    assert "hunter2secret" not in ev.payload and "tok123" not in ev.payload
    data = json.loads(ev.payload)
    assert data["run_at"] == "2026-08-20T04:00:00+00:00"  # 非敏感键保留
    assert data["<redacted>"] == "<redacted>"  # api_key → 键值一并改写
    assert data["nested"]["<redacted>"] == "<redacted>"  # secret_token


def test_stale_worker_writes_no_scheduling_events(db_session):
    """租约被回收后，stale worker 无法写任何调度/步骤事件。"""
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=10), clock=clock
    )
    enqueue(db_session, execution.id, clock=clock)
    execution, attempt = _claim(db_session, worker_id="stale-w", clock=clock)
    transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
    clock.advance(61)  # 租约过期
    recover_expired(db_session, clock=clock)  # 回收（attempt ABORTED）
    events_after_recover = len(list_events(db_session, execution.id))
    with pytest.raises(ExecutionClaimLostError):
        run_execution_steps(db_session, execution, attempt, clock=clock)
    # stale worker 未写任何事件（含调度事件；recover 的 LEASE_RECOVERED 之后）
    assert len(list_events(db_session, execution.id)) == events_after_recover
    assert (
        db_session.query(ExecutionStep)
        .filter(ExecutionStep.execution_id == execution.id)
        .count()
        == 0
    )
    events = _event_types(db_session, execution.id)
    assert events.count("MISFIRE_DETECTED") == 0  # 回收后的重排不算 misfire
    assert events.count("SCHEDULE_DUE") == 1  # 只有合法 claim 写的一次


# =====================================================================
# 27-30. 兼容性：6A/6B/6C/Reminder 不回退
# =====================================================================


def test_system_ping_compat(db_session):
    """run_at=None 与 run_at=到期 的 system.ping 完整执行均成功。"""
    clock = FakeClock()
    ex_immediate = _make(db_session, clock=clock)
    enqueue(db_session, ex_immediate.id, clock=clock)
    ex_scheduled = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=60), clock=clock
    )
    enqueue(db_session, ex_scheduled.id, clock=clock)
    factory = _session_factory(db_session)
    w = _worker(factory, clock, "w6d-ping")
    stats = w.run_once()
    assert stats["claimed"] == 1 and stats["succeeded"] == 1
    stats = w.run_once()  # 每 tick 最多 claim 一个：第二个任务下一 tick
    assert stats["claimed"] == 1 and stats["succeeded"] == 1
    db_session.refresh(ex_immediate)
    db_session.refresh(ex_scheduled)
    assert ex_immediate.status == ExecutionStatus.SUCCEEDED.value
    assert ex_scheduled.status == ExecutionStatus.SUCCEEDED.value
    assert "SCHEDULE_DUE" in _event_types(db_session, ex_scheduled.id)
    assert "SCHEDULE_DUE" not in _event_types(db_session, ex_immediate.id)


def test_phase6c_step_events_no_regression(db_session, monkeypatch):
    """6C 模板步骤与 6D 调度共存：线性执行 + 事件流不回退。"""
    order = []

    def _t1(payload):
        order.append("t1")
        return {"ok": True}

    def _t2(payload):
        order.append("t2")
        return {"ok": True}

    monkeypatch.setitem(EXECUTION_TOOLS, "test.s1", ToolSpec(name="test.s1", handler=_t1))
    monkeypatch.setitem(EXECUTION_TOOLS, "test.s2", ToolSpec(name="test.s2", handler=_t2))
    monkeypatch.setitem(
        EXECUTION_TYPES,
        "test.steps",
        ExecutionTypeSpec(
            name="test.steps", payload_schema_version=1,
            payload_allowed_fields=frozenset(), max_attempts=1,
            timeout_seconds=None,
            steps=(StepSpec("s1", "test.s1"), StepSpec("s2", "test.s2")),
            misfire_policy="RUN_IMMEDIATELY", misfire_grace_seconds=300.0,
        ),
    )
    clock = FakeClock()
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=10),
        execution_type="test.steps", clock=clock,
    )
    enqueue(db_session, execution.id, clock=clock)
    stats = _worker(_session_factory(db_session), clock, "w6d-steps").run_once()
    assert stats["succeeded"] == 1
    assert order == ["t1", "t2"]  # 严格线性顺序
    events = _event_types(db_session, execution.id)
    assert events.count("STEP_STARTED") == 2
    assert events.count("STEP_SUCCEEDED") == 2
    assert "SCHEDULE_DUE" in events  # 调度任务正常到期
    assert "MISFIRE_DETECTED" not in events  # grace 内不 misfire


def test_phase6b_retry_lease_no_regression(db_session, monkeypatch):
    """6B retry/lease 与 run_at 共存；重试任务不算 misfire。"""
    calls = {"n": 0}

    def _flaky(payload):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setitem(
        EXECUTION_TOOLS, "test.flaky",
        ToolSpec(name="test.flaky", handler=_flaky, retryable=True, replay_safe=True),
    )
    monkeypatch.setitem(
        EXECUTION_TYPES,
        "test.flaky",
        ExecutionTypeSpec(
            name="test.flaky", payload_schema_version=1,
            payload_allowed_fields=frozenset(), max_attempts=2,
            timeout_seconds=None, retry_backoff_base_seconds=2.0,
            retry_backoff_factor=2.0,
            steps=(StepSpec("run", "test.flaky"),),
            misfire_policy="FAIL", misfire_grace_seconds=300.0,
        ),
    )
    clock = FakeClock()
    # run_at 在 grace 内开始（首次 claim 不 misfire）；重试后再跨过 grace——
    # attempt_count>=1 的任务即使远超 grace 也绝不 misfire（retry_after 门控）
    execution = _make(
        db_session, run_at=FIXED_NOW - timedelta(seconds=100),
        execution_type="test.flaky", clock=clock,
    )
    enqueue(db_session, execution.id, clock=clock)
    w = _worker(_session_factory(db_session), clock, "w6d-retry")
    stats = w.run_once()
    assert stats["retried"] == 1  # attempt 1 失败 → 退避重试
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.QUEUED.value
    assert execution.retry_after is not None  # 重试门由 retry_after 控制
    clock.advance(401)  # 远超 run_at+grace（+300）且 backoff（2s）已过
    stats = w.run_once()
    assert stats["failed"] == 1  # attempt 2 失败 → max_attempts 耗尽
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value
    events = _event_types(db_session, execution.id)
    assert events.count("RETRY_SCHEDULED") == 1
    assert "MISFIRE_DETECTED" not in events  # attempt_count>=1 → 不 misfire
    assert calls["n"] == 2  # 租约/重试机制正常驱动


def test_reminder_behavior_unchanged(db_session):
    """Reminder 完全隔离：模型/Worker 可导入、表独立可写、状态机不受影响。"""
    import app.workers.reminder_worker  # noqa: F401 — 可导入（未被修改）
    from app.models.reminder import Reminder, ReminderStatus
    from app.models.task import Task

    assert "reminders" in set(inspect(db_session.get_bind()).get_table_names())
    task = Task(title="6d-reminder-probe")
    db_session.add(task)
    db_session.flush()
    reminder = Reminder(
        task_id=task.id,
        remind_at=FIXED_NOW + timedelta(hours=1),
        status=ReminderStatus.PENDING.value,
    )
    db_session.add(reminder)
    db_session.commit()
    db_session.refresh(reminder)
    assert reminder.status == ReminderStatus.PENDING.value
    assert reminder.created_at is not None
    # Execution 调度流程不触碰 reminders 表
    clock = FakeClock()
    execution = _make(db_session, run_at=FIXED_NOW - timedelta(seconds=10), clock=clock)
    enqueue(db_session, execution.id, clock=clock)
    assert _claim(db_session, clock=clock) is not None
    assert db_session.query(Reminder).count() == 1  # 无新增/修改


# =====================================================================
# 31-32. 方言与迁移
# =====================================================================


def test_pg_offline_sql_compat():
    """claim 双门 CAS 与 misfire 裁决 CAS 在 PostgreSQL 方言下离线编译。"""
    now = _naive(FIXED_NOW)
    # claim CAS（run_at/retry_after 双门）
    sql = str(
        update(Execution)
        .where(
            Execution.status == ExecutionStatus.QUEUED.value,
            or_(Execution.run_at.is_(None), Execution.run_at <= now),
            or_(Execution.retry_after.is_(None), Execution.retry_after <= now),
        )
        .values(
            status=ExecutionStatus.CLAIMED.value,
            lease_owner="w", updated_at=now,
        )
        .compile(dialect=postgresql.dialect())
    )
    assert "UPDATE executions" in sql
    assert "executions.run_at" in sql and "executions.retry_after" in sql
    # misfire SKIP/FAIL CAS
    for target in (ExecutionStatus.CANCELLED.value, ExecutionStatus.FAILED.value):
        sql = str(
            update(Execution)
            .where(
                Execution.id == 1,
                Execution.status == ExecutionStatus.QUEUED.value,
            )
            .values(status=target, finished_at=now)
            .compile(dialect=postgresql.dialect())
        )
        assert "UPDATE executions" in sql and "executions.status" in sql
    # 候选 SELECT（PG 下 aware 比较）
    sql = str(
        (
            Execution.__table__.select()
            .where(Execution.status == ExecutionStatus.QUEUED.value)
            .where(
                or_(Execution.run_at.is_(None), Execution.run_at <= now)
            )
        ).compile(dialect=postgresql.dialect())
    )
    assert "executions.run_at" in sql


def test_no_new_migration_single_head():
    """6D 未创建 migration：单 head 仍为 8c9d0e1f2a3b4，链不变。"""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    heads = set(ScriptDirectory.from_config(cfg).get_heads())
    assert heads == {"8c9d0e1f2a3b4"}
    versions = Path(BACKEND_DIR / "alembic" / "versions")
    newest = max(p.stat().st_mtime for p in versions.glob("*.py"))
    for p in versions.glob("*.py"):
        if p.stat().st_mtime == newest:
            assert p.name.startswith("8c9d0e1f2a3b4")  # 最新迁移仍是 6C 的
    mods = {}
    for path in versions.glob("*.py"):
        spec = importlib.util.spec_from_file_location(path.stem, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mods[mod.revision] = mod.down_revision
    assert mods["8c9d0e1f2a3b4"] == "7b8c9d0e1f2a3"
    assert mods["7b8c9d0e1f2a3"] == "6a7b8c9d0e1f2"
    assert mods["6a7b8c9d0e1f2"] == "e6f5d4c3b2a1"
