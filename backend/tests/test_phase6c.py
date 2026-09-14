"""Phase 6C 定向测试：Step Orchestration and Tool Safety。

覆盖需求清单（27 项）：
1. 声明式 Tool/ExecutionType Registry（fail-closed，无动态代码路径）
2. ToolSpec 构造期安全规则（destructive ⇒ confirmation；replay_safe=False）
3. 工具白名单解析（未知工具 EXEC_TOOL_UNKNOWN；禁用工具 EXEC_TOOL_NOT_ALLOWED）
4. 输入/输出 schema 校验（unknown fields 默认拒绝，strict）
5. system.ping 兼容（显式单步，result_text 语义不变）
6. 事件流生命周期（7 事件有序序列，同事务写入）
7. 事件 append-only 契约（服务层无 UPDATE/DELETE 路径；UNIQUE 序列约束）
8. 事务内 MAX+1 序列单调；并发冲突 → EXEC_EVENT_CONFLICT 回滚
9. 事件 payload 脱敏（敏感字段名 → <redacted>，长字符串截断）
10. 未知事件类型 → ValueError（白名单精确 17 种）
11. 线性步骤编排（按 step_index 顺序；前一步成功才继续）
12. 步骤失败即止（后续步骤不物化）+ 既有 attempt 重试（无第二套 retry）
13. 6A 状态机仍是最终裁决者（retryable 门控；稳定失败不重试）
14. 输入来源（payload / literal）+ input_hash 绑定稳定性
15. confirmation gate：无 GRANTED 绝不执行（handler 零调用）
16. grant / reject 语义（GRANTED 放行；REJECTED 稳定失败）
17. 确认绑定 input_hash；hash 变化 → EXPIRED（绝不跨 hash 复用）
18. 确认仅限 QUEUED/CLAIMED/RUNNING；显式 actor，无默认确认
19. 确认服务可先于 Worker 物化步骤
20. 安全恢复：SUCCEEDED 跳过（绝不重跑）；RUNNING 按 replay_safe 裁决
21. destructive/replay-unsafe 崩溃后绝不自动重放（稳定失败）
22. stale worker 第一次写入即终止（EXEC_CLAIM_LOST，不写 step/event/终态）
23. 终态 execution 拒绝再编排
24. 敏感输入 fail-closed：任何落盘之前拒绝（秘密绝不入库）
25. 输出/错误脱敏（<redacted>、500 字符上限、无 traceback）
26. 步骤输入截断存储（2000 字符）但完整输入参与 hash 与执行
27. 表约束：双 UNIQUE、FK CASCADE/SET NULL、事件序列 UNIQUE
28. 迁移回环（upgrade → downgrade → re-upgrade）+ PG 离线 SQL + 单 head

全部离线：临时 SQLite + FK pragma + create_all；FakeClock 可推进；
monkeypatch 注册表（自动恢复）；绝不访问 jarvis.db / .env。
"""

import itertools
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.clock import FixedClock
from app.core.database import Base, _set_sqlite_pragma
from app.models.execution import Execution, ExecutionStatus
from app.models.execution_attempt import AttemptStatus, ExecutionAttempt
from app.models.execution_event import ExecutionEvent
from app.models.execution_step import (
    ConfirmationStatus,
    ExecutionStep,
    StepStatus,
)
from app.services.execution_claim import claim_next, recover_expired
from app.services.execution_errors import (
    EventConflictError,
    ExecutionClaimLostError,
    InvalidExecutionTransitionError,
    StepInvalidError,
    StepSchemaError,
    ToolUnknownError,
)
from app.services.execution_events import (
    APPEND_ONLY_EVENT_TYPES,
    append_event,
    list_events,
)
from app.services.execution_registry import (
    EXECUTION_TOOLS,
    EXECUTION_TYPES,
    ExecutionTypeSpec,
    PingInput,
    PingOutput,
    StepSpec,
    ToolSpec,
    get_tool,
    resolve_step_template,
)
from app.services.execution_service import (
    create_execution,
    enqueue,
    transition_to,
)
from app.services.execution_steps import (
    STEP_INPUT_MAX_CHARS,
    _hash_of,
    grant_confirmation,
    reject_confirmation,
    run_execution_steps,
)
from app.services.execution_worker import ExecutionWorker

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 8, 20, 4, 0, tzinfo=timezone.utc)
PING = {}


class FakeClock:
    """可推进时钟（6C 需要模拟 lease 过期 / backoff 时间流逝）。"""

    def __init__(self, start=FIXED_NOW):
        self._now = start

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)


def _clock():
    return FixedClock(FIXED_NOW)


_key_counter = itertools.count(1)


def _unique_key() -> str:
    return f"test-6c-key-{next(_key_counter):08d}"


@pytest.fixture()
def db_session(tmp_path):
    db_file = tmp_path / "test_6c.db"
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
        clock=_clock(),
        **kwargs,
    )


def _claim(db, worker_id="worker-a", clock=None):
    return claim_next(db, worker_id, clock=clock or _clock(), lease_ttl_seconds=60)


def _start_claimed(db, execution_id, worker_id="w1", clock=None):
    """service 级直接编排入口：enqueue → claim → RUNNING。"""
    clock = clock or _clock()
    enqueue(db, execution_id, clock=clock)
    execution, attempt = _claim(db, worker_id, clock=clock)
    transition_to(db, execution.id, ExecutionStatus.RUNNING, clock=clock)
    db.refresh(execution)
    return execution, attempt


def _event_types(db, execution_id):
    return [e.event_type for e in list_events(db, execution_id)]


# ---- 测试用 schema（strict + extra=forbid，与生产同源策略） ----


class _TestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _NumInput(_TestModel):
    value: int


class _NumOutput(_TestModel):
    ok: bool


# ---- 注册表隔离：monkeypatch 自动恢复 ----


def _register_type(
    monkeypatch,
    name,
    *,
    max_attempts=3,
    timeout_seconds=None,
    steps=(),
    payload_fields=frozenset(),
    base=0.001,
    factor=2.0,
):
    monkeypatch.setitem(
        EXECUTION_TYPES,
        name,
        ExecutionTypeSpec(
            name=name,
            payload_schema_version=1,
            payload_allowed_fields=frozenset(payload_fields),
            max_attempts=max_attempts,
            timeout_seconds=timeout_seconds,
            retry_backoff_base_seconds=base,
            retry_backoff_factor=factor,
            steps=tuple(steps),
        ),
    )


def _register_tool(
    monkeypatch,
    name,
    *,
    handler=None,
    input_schema=None,
    output_schema=None,
    destructive=False,
    requires_confirmation=False,
    retryable=True,
    replay_safe=True,
    sanitize_result=True,
    enabled=True,
):
    monkeypatch.setitem(
        EXECUTION_TOOLS,
        name,
        ToolSpec(
            name=name,
            handler=handler,
            input_schema=input_schema,
            output_schema=output_schema,
            destructive=destructive,
            requires_confirmation=requires_confirmation,
            retryable=retryable,
            replay_safe=replay_safe,
            sanitize_result=sanitize_result,
            enabled=enabled,
        ),
    )


def _make_config(db_file):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def _step_by_key(db, execution_id, step_key):
    return (
        db.query(ExecutionStep)
        .filter(
            ExecutionStep.execution_id == execution_id,
            ExecutionStep.step_key == step_key,
        )
        .one()
    )


# =====================================================================
# 1. Registry：声明式白名单 + fail-closed + 无动态代码路径
# =====================================================================


def test_tool_registry_declarative_whitelist(db_session):
    tool = EXECUTION_TOOLS["system.ping"]
    assert callable(tool.handler)
    assert tool.input_schema is PingInput
    assert tool.output_schema is PingOutput
    assert tool.destructive is False
    assert tool.requires_confirmation is False
    assert tool.retryable is True
    assert tool.replay_safe is True
    assert tool.sanitize_result is True
    assert tool.enabled is True
    # system.ping 显式单步模板（step_key="ping"，工具 = 类型同名）
    spec = EXECUTION_TYPES["system.ping"]
    assert spec.steps == (StepSpec(step_key="ping", tool_name="system.ping"),)
    assert resolve_step_template(spec) == spec.steps
    # legacy 合成（无 steps 模板 → 单步 run）
    legacy = ExecutionTypeSpec(
        name="legacy.t", payload_schema_version=1,
        payload_allowed_fields=frozenset(), max_attempts=3, timeout_seconds=None,
    )
    assert resolve_step_template(legacy) == (
        StepSpec(step_key="run", tool_name="legacy.t"),
    )


def test_toolspec_construction_safety_rules(db_session):
    # destructive 未声明 confirmation → 构造期 ValueError（绝不自动视为安全）
    with pytest.raises(ValueError):
        ToolSpec(name="x", destructive=True)
    # destructive + replay_safe 默认 True → ValueError（绝不自 动重放）
    with pytest.raises(ValueError):
        ToolSpec(name="x", destructive=True, requires_confirmation=True)
    # 合规 destructive：confirmation + replay_safe=False
    tool = ToolSpec(
        name="x", destructive=True, requires_confirmation=True, replay_safe=False
    )
    assert tool.requires_confirmation is True
    assert tool.replay_safe is False
    # 非 destructive 显式 requires_confirmation 合法
    assert ToolSpec(name="y", requires_confirmation=True).destructive is False
    # 默认全 False：destructive 默认不开启
    assert ToolSpec(name="z").destructive is False


def test_get_tool_fail_closed(db_session):
    # 步骤模式：未知工具 → EXEC_TOOL_UNKNOWN（稳定，不重试）
    with pytest.raises(ToolUnknownError) as ei:
        get_tool("no.such.tool", legacy_fallback=False)
    assert ei.value.code == "EXEC_TOOL_UNKNOWN"
    assert ei.value.retryable is False
    # legacy 兼容：6B handler 表仍是 fallback（EXEC_HANDLER_ERROR 语义）
    with pytest.raises(Exception) as e2:
        get_tool("no.such.tool", legacy_fallback=True)
    assert e2.value.code == "EXEC_HANDLER_ERROR"


def test_registry_source_has_no_dynamic_code_paths(db_session):
    """handler 解析路径不得包含字符串 import/eval/exec/反射（静态断言）。"""
    for mod_path in (
        BACKEND_DIR / "app" / "services" / "execution_steps.py",
        BACKEND_DIR / "app" / "services" / "execution_registry.py",
    ):
        src = mod_path.read_text(encoding="utf-8")
        assert "eval(" not in src
        assert "exec(" not in src
        assert "importlib" not in src
        assert "__import__" not in src
    # 工具解析绝不走反射（仅 registry 模块内无 getattr 引用）
    src = (BACKEND_DIR / "app" / "services" / "execution_registry.py").read_text(
        encoding="utf-8"
    )
    assert "getattr(" not in src


# =====================================================================
# 2. 工具白名单执行：禁用 → 稳定失败；handler 零调用
# =====================================================================


def test_disabled_tool_refused_stable_failure(db_session, monkeypatch):
    calls = {"n": 0}

    def _h(payload):
        calls["n"] += 1
        return {"ok": True}

    _register_tool(monkeypatch, "sec.disable", handler=_h, enabled=False)
    _register_type(
        monkeypatch, "sec.type", max_attempts=3,
        steps=(StepSpec(step_key="run", tool_name="sec.disable"),),
    )
    execution = _make(db_session, execution_type="sec.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    stats = worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.last_error_code == "EXEC_TOOL_NOT_ALLOWED"
    assert calls["n"] == 0  # handler 绝不调用
    assert "RETRY_SCHEDULED" not in _event_types(db_session, execution.id)
    assert stats["failed"] == 1


# =====================================================================
# 3. Schema 校验：unknown fields 默认拒绝（strict + extra=forbid）
# =====================================================================


def test_input_schema_rejects_bad_inputs(db_session, monkeypatch):
    calls = {"n": 0}

    def _h(payload):
        calls["n"] += 1
        return {"ok": True}

    _register_tool(
        monkeypatch, "sch.tool", handler=_h, input_schema=_NumInput,
        output_schema=_NumOutput,
    )
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    # ① 缺失必填字段（payload {} 无 value）
    _register_type(
        monkeypatch, "sch.type", max_attempts=1,
        steps=(StepSpec(step_key="s", tool_name="sch.tool"),),
    )
    execution = _make(db_session, execution_type="sch.type")
    enqueue(db_session, execution.id)
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.last_error_code == "EXEC_SCHEMA_INVALID"
    # ② 字段类型错误
    _register_type(
        monkeypatch, "sch.type", max_attempts=1,
        steps=(
            StepSpec(step_key="s", tool_name="sch.tool",
                     input_source="literal", input_literal={"value": "not-int"}),
        ),
    )
    execution2 = _make(db_session, execution_type="sch.type")
    enqueue(db_session, execution2.id)
    worker.run_once()
    db_session.refresh(execution2)
    assert execution2.status == ExecutionStatus.FAILED.value
    assert execution2.last_error_code == "EXEC_SCHEMA_INVALID"
    # ③ unknown field（extra=forbid 默认拒绝）
    _register_type(
        monkeypatch, "sch.type", max_attempts=1,
        steps=(
            StepSpec(step_key="s", tool_name="sch.tool",
                     input_source="literal", input_literal={"value": 1, "extra": 2}),
        ),
    )
    execution3 = _make(db_session, execution_type="sch.type")
    enqueue(db_session, execution3.id)
    worker.run_once()
    db_session.refresh(execution3)
    assert execution3.status == ExecutionStatus.FAILED.value
    assert execution3.last_error_code == "EXEC_SCHEMA_INVALID"
    assert calls["n"] == 0  # 校验失败绝不调用 handler
    step = _step_by_key(db_session, execution.id, "s")
    assert step.status == StepStatus.FAILED.value
    assert step.error_code == "EXEC_SCHEMA_INVALID"


def test_output_schema_rejects_bad_outputs(db_session, monkeypatch):
    def _bad_type(payload):
        return {"ok": "not-a-bool"}

    def _extra_field(payload):
        return {"ok": True, "extra": 1}

    for idx, handler in enumerate((_bad_type, _extra_field)):
        _register_tool(
            monkeypatch, f"out.tool{idx}", handler=handler,
            input_schema=_NumInput, output_schema=_NumOutput,
        )
        _register_type(
            monkeypatch, f"out.type{idx}", max_attempts=1,
            steps=(
                StepSpec(step_key="s", tool_name=f"out.tool{idx}",
                         input_source="literal", input_literal={"value": 1}),
            ),
        )
        execution = _make(db_session, execution_type=f"out.type{idx}")
        enqueue(db_session, execution.id)
        worker = ExecutionWorker(_session_factory(db_session), worker_id=f"w{idx}")
        worker.run_once()
        db_session.refresh(execution)
        assert execution.status == ExecutionStatus.FAILED.value
        assert execution.last_error_code == "EXEC_SCHEMA_INVALID"


def test_system_ping_step_level_semantics(db_session):
    """6C 编排与 6B 逐字节等价：单步 ping 输出一致。"""
    execution = _make(db_session)
    execution, attempt = _start_claimed(db_session, execution.id)
    result = run_execution_steps(db_session, execution, attempt, clock=_clock())
    assert result == {"pong": True}
    step = _step_by_key(db_session, execution.id, "ping")
    assert step.step_index == 0
    assert step.tool_name == "system.ping"
    assert step.status == StepStatus.SUCCEEDED.value
    assert step.attempt_id == attempt.id
    assert step.input_hash == _hash_of({})
    assert json.loads(step.output_json) == {"pong": True}


# =====================================================================
# 4. 事件流：生命周期 / append-only / 序列 / 脱敏 / 白名单
# =====================================================================


def test_event_lifecycle_sequence(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    types = _event_types(db_session, execution.id)
    assert types == [
        "EXECUTION_CREATED",
        "EXECUTION_QUEUED",
        "EXECUTION_CLAIMED",
        "EXECUTION_STARTED",
        "STEP_STARTED",
        "STEP_SUCCEEDED",
        "EXECUTION_SUCCEEDED",
    ]
    events = list_events(db_session, execution.id)
    for i, ev in enumerate(events, start=1):
        assert ev.sequence_number == i
        assert ev.actor_type in ("service", "worker")
        assert ev.actor_id
    # STEP_STARTED 携带步骤上下文；STEP_SUCCEEDED 携带输出指纹
    started = next(e for e in events if e.event_type == "STEP_STARTED")
    payload = json.loads(started.payload)
    assert payload["step"] == "ping"  # 事件 payload 键避开 *_key 脱敏启发式
    assert payload["step_index"] == 0
    assert payload["tool_name"] == "system.ping"
    assert payload["attempt_number"] == 1
    assert payload["input_hash"] == _hash_of({})
    done = next(e for e in events if e.event_type == "STEP_SUCCEEDED")
    assert json.loads(done.payload)["output_hash"] == _hash_of({"pong": True})


def test_events_append_only_contract(db_session):
    """服务层无 UPDATE/DELETE 路径（模块源码断言）+ 序列唯一约束。"""
    src = (
        BACKEND_DIR / "app" / "services" / "execution_events.py"
    ).read_text(encoding="utf-8")
    assert "update(" not in src
    assert "delete(" not in src
    execution = _make(db_session)
    # DB 级仲裁：同 (execution_id, sequence_number) 直接 INSERT → IntegrityError
    append_event(
        db_session, execution_id=execution.id, event_type="EXECUTION_QUEUED",
        clock=_clock(),
    )
    db_session.commit()
    dup = ExecutionEvent(
        execution_id=execution.id,
        event_type="EXECUTION_QUEUED",
        sequence_number=1,  # 与 EXECUTION_CREATED 的序列冲突
        actor_type="test",
        actor_id="dup",
        created_at=FIXED_NOW,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_event_sequence_monotonic_within_transaction(db_session):
    execution = _make(db_session)
    for _ in range(3):
        append_event(
            db_session, execution_id=execution.id,
            event_type="EXECUTION_QUEUED", clock=_clock(),
        )
    db_session.commit()
    seqs = [e.sequence_number for e in list_events(db_session, execution.id)]
    assert seqs == [1, 2, 3, 4]  # 含 EXECUTION_CREATED


def test_event_payload_sanitized(db_session):
    execution = _make(db_session)
    append_event(
        db_session,
        execution_id=execution.id,
        event_type="EXECUTION_FAILED",
        payload={
            "api_key": "sk-super-secret-123",
            "nested": {"token": "tok-456"},
            "long": "x" * 2000,
            "safe": True,
        },
        clock=_clock(),
    )
    db_session.commit()
    ev = list_events(db_session, execution.id)[-1]
    stored = json.loads(ev.payload)
    # deny 词表命中：键与值一并改写为 <redacted>（绝不落盘）；长字符串截断 500
    assert stored == {
        "<redacted>": "<redacted>",
        "nested": {"<redacted>": "<redacted>"},
        "long": "x" * 500,
        "safe": True,
    }
    assert "sk-super-secret-123" not in ev.payload
    assert "tok-456" not in ev.payload


def test_unknown_event_type_rejected(db_session):
    execution = _make(db_session)
    with pytest.raises(ValueError):
        append_event(
            db_session, execution_id=execution.id, event_type="BOGUS_EVENT",
            clock=_clock(),
        )
    db_session.rollback()
    # 白名单精确 23 种（6C 的 17 种 + 6D 授权的 6 种一次性调度事件）
    assert APPEND_ONLY_EVENT_TYPES == frozenset(
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
    # 失败未污染事务：后续写入正常
    append_event(
        db_session, execution_id=execution.id, event_type="EXECUTION_QUEUED",
        clock=_clock(),
    )
    db_session.commit()
    assert len(list_events(db_session, execution.id)) == 2


def test_event_sequence_conflict_rolls_back_transaction(db_session, monkeypatch):
    execution = _make(db_session)  # 事件 1: EXECUTION_CREATED
    append_event(
        db_session, execution_id=execution.id, event_type="EXECUTION_QUEUED",
        clock=_clock(),
    )
    db_session.commit()
    # 模拟并发竞争者：序列号产生器返回已占用值 → UNIQUE 仲裁
    monkeypatch.setattr(
        "app.services.execution_events._next_sequence", lambda db, eid: 1
    )
    with pytest.raises(EventConflictError) as ei:
        append_event(
            db_session, execution_id=execution.id,
            event_type="EXECUTION_STARTED", clock=_clock(),
        )
    assert ei.value.code == "EXEC_EVENT_CONFLICT"
    db_session.rollback()
    assert len(list_events(db_session, execution.id)) == 2  # 冲突事件未残留


def test_transition_failure_writes_no_event(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    transition_to(db_session, execution.id, ExecutionStatus.CLAIMED, clock=_clock())
    transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=_clock())
    transition_to(db_session, execution.id, ExecutionStatus.SUCCEEDED, clock=_clock())
    before = len(list_events(db_session, execution.id))
    with pytest.raises(InvalidExecutionTransitionError):
        transition_to(db_session, execution.id, ExecutionStatus.FAILED, clock=_clock())
    assert len(list_events(db_session, execution.id)) == before  # 无悬空事件


# =====================================================================
# 5. 步骤编排：线性顺序 / 失败即止 / 既有 retry / 6A 终裁
# =====================================================================


def test_steps_execute_in_order_linear(db_session, monkeypatch):
    order = []

    def _mk(name):
        def _h(payload):
            order.append(name)
            return {"step": name}

        return _h

    for name in ("a", "b", "c"):
        _register_tool(monkeypatch, f"ord.tool{name}", handler=_mk(name))
    _register_type(
        monkeypatch, "ord.type", max_attempts=1,
        steps=(
            StepSpec(step_key="s1", tool_name="ord.toola"),
            StepSpec(step_key="s2", tool_name="ord.toolb"),
            StepSpec(step_key="s3", tool_name="ord.toolc"),
        ),
    )
    execution = _make(db_session, execution_type="ord.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert order == ["a", "b", "c"]  # 严格线性、按序
    steps = (
        db_session.query(ExecutionStep)
        .filter(ExecutionStep.execution_id == execution.id)
        .order_by(ExecutionStep.step_index)
        .all()
    )
    assert [s.status for s in steps] == [StepStatus.SUCCEEDED.value] * 3
    assert [s.step_index for s in steps] == [0, 1, 2]
    attempt = (
        db_session.query(ExecutionAttempt)
        .filter(ExecutionAttempt.execution_id == execution.id)
        .one()
    )
    assert json.loads(attempt.result_text) == {"step": "c"}  # 最后一步输出
    types = _event_types(db_session, execution.id)
    assert types.count("STEP_STARTED") == 3
    assert types.count("STEP_SUCCEEDED") == 3


def test_step_failure_halts_later_steps(db_session, monkeypatch):
    def _boom(payload):
        raise RuntimeError("s2 boom")

    _register_tool(monkeypatch, "halt.a", handler=lambda p: {"ok": True})
    _register_tool(monkeypatch, "halt.b", handler=_boom)
    _register_tool(monkeypatch, "halt.c", handler=lambda p: {"ok": True})
    _register_type(
        monkeypatch, "halt.type", max_attempts=1,  # 单次：耗尽即终态
        steps=(
            StepSpec(step_key="s1", tool_name="halt.a"),
            StepSpec(step_key="s2", tool_name="halt.b"),
            StepSpec(step_key="s3", tool_name="halt.c"),
        ),
    )
    execution = _make(db_session, execution_type="halt.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.last_error_code == "EXEC_HANDLER_ERROR"
    steps = (
        db_session.query(ExecutionStep)
        .filter(ExecutionStep.execution_id == execution.id)
        .order_by(ExecutionStep.step_index)
        .all()
    )
    assert [s.status for s in steps] == [
        StepStatus.SUCCEEDED.value,
        StepStatus.FAILED.value,
    ]
    assert len(steps) == 2  # s3 从未物化：失败即止
    assert steps[1].error_code == "EXEC_HANDLER_ERROR"
    types = _event_types(db_session, execution.id)
    assert "STEP_FAILED" in types
    assert "EXECUTION_FAILED" in types


def test_step_retry_uses_existing_attempt_system(db_session, monkeypatch):
    calls = {"n": 0}

    def _flaky(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    _register_tool(monkeypatch, "ret.tool", handler=_flaky)
    _register_type(
        monkeypatch, "ret.type", max_attempts=3, base=0.001, factor=2.0,
        steps=(StepSpec(step_key="s", tool_name="ret.tool"),),
    )
    execution = _make(db_session, execution_type="ret.type")
    enqueue(db_session, execution.id)
    clock = FakeClock()
    worker = ExecutionWorker(
        _session_factory(db_session), worker_id="w1", clock=clock
    )
    stats1 = worker.run_once()
    assert stats1["retried"] == 1
    clock.advance(1)  # backoff 0.001s 已到期
    stats2 = worker.run_once()
    assert stats2["succeeded"] == 1
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    attempts = (
        db_session.query(ExecutionAttempt)
        .filter(ExecutionAttempt.execution_id == execution.id)
        .order_by(ExecutionAttempt.attempt_number)
        .all()
    )
    assert [a.attempt_number for a in attempts] == [1, 2]  # 既有 attempt 机制
    assert [a.status for a in attempts] == [
        AttemptStatus.FAILED.value,
        AttemptStatus.SUCCEEDED.value,
    ]
    types = _event_types(db_session, execution.id)
    assert "RETRY_SCHEDULED" in types  # 单一 retry 系统的事件
    retry_ev = next(
        e for e in list_events(db_session, execution.id)
        if e.event_type == "RETRY_SCHEDULED"
    )
    assert json.loads(retry_ev.payload)["attempt_number"] == 1
    # 同一步骤在两个 attempt 上各执行一次（FAILED 步骤可随 attempt 重试）
    assert types.count("STEP_STARTED") == 2


def test_retryable_false_stable_failure_6a_decides(db_session, monkeypatch):
    """retryable=False：即使 attempt 未耗尽也不重试——6A 状态机终裁 FAILED。"""
    _register_tool(
        monkeypatch, "stable.tool", handler=lambda p: {"ok": True},
        input_schema=_NumInput,
    )
    _register_type(
        monkeypatch, "stable.type", max_attempts=5,
        steps=(
            StepSpec(step_key="s", tool_name="stable.tool",
                     input_source="literal", input_literal={"value": "bad"}),
        ),
    )
    execution = _make(db_session, execution_type="stable.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value  # 终态，无重试
    assert execution.last_error_code == "EXEC_SCHEMA_INVALID"
    attempts = (
        db_session.query(ExecutionAttempt)
        .filter(ExecutionAttempt.execution_id == execution.id)
        .all()
    )
    assert len(attempts) == 1  # 单 attempt：稳定失败绝不重试
    assert "RETRY_SCHEDULED" not in _event_types(db_session, execution.id)


def test_literal_input_source_and_hash_binding(db_session, monkeypatch):
    received = {}

    def _h(payload):
        received["input"] = payload
        return {"ok": True}

    _register_tool(monkeypatch, "lit.tool", handler=_h)
    _register_type(
        monkeypatch, "lit.type", max_attempts=1,
        steps=(
            StepSpec(step_key="s", tool_name="lit.tool",
                     input_source="literal",
                     input_literal={"alpha": 1, "beta": "x"}),
        ),
    )
    execution = _make(db_session, execution_type="lit.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert received["input"] == {"alpha": 1, "beta": "x"}  # handler 收到完整字面量
    step = _step_by_key(db_session, execution.id, "s")
    # hash 绑定规范化输入（键排序无关）
    assert step.input_hash == _hash_of({"beta": "x", "alpha": 1})


def test_terminal_execution_refuses_reorchestration(db_session):
    execution = _make(db_session)
    execution, attempt = _start_claimed(db_session, execution.id)
    transition_to(db_session, execution.id, ExecutionStatus.SUCCEEDED, clock=_clock())
    events_before = len(list_events(db_session, execution.id))
    with pytest.raises(StepInvalidError):
        run_execution_steps(db_session, execution, attempt, clock=_clock())
    # 终态拒绝先于任何写入
    assert len(list_events(db_session, execution.id)) == events_before


# =====================================================================
# 6. Confirmation gate：无 GRANTED 绝不执行 / grant / reject / 绑定
# =====================================================================


def test_destructive_tool_blocks_without_confirmation(db_session, monkeypatch):
    calls = {"n": 0}

    def _danger(payload):
        calls["n"] += 1
        return {"done": True}

    _register_tool(
        monkeypatch, "sec.tool", handler=_danger,
        destructive=True, requires_confirmation=True, replay_safe=False,
    )
    _register_type(
        monkeypatch, "sec.type", max_attempts=3,
        steps=(StepSpec(step_key="wipe", tool_name="sec.tool"),),
    )
    execution = _make(db_session, execution_type="sec.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    stats = worker.run_once()
    assert stats["retried"] == 1  # 等待确认：可重试（受 max_attempts 约束）
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.QUEUED.value
    assert calls["n"] == 0  # 无 GRANTED 绝不调用 handler
    step = _step_by_key(db_session, execution.id, "wipe")
    assert step.status == StepStatus.FAILED.value
    assert step.error_code == "EXEC_CONFIRMATION_REQUIRED"
    assert step.requires_confirmation is True
    assert step.confirmation_status == ConfirmationStatus.PENDING.value
    types = _event_types(db_session, execution.id)
    assert "CONFIRMATION_REQUIRED" in types
    assert "STEP_FAILED" in types
    assert "RETRY_SCHEDULED" in types


def test_grant_then_executes(db_session, monkeypatch):
    calls = {"n": 0}

    def _danger(payload):
        calls["n"] += 1
        return {"done": True}

    _register_tool(
        monkeypatch, "g.tool", handler=_danger,
        destructive=True, requires_confirmation=True, replay_safe=False,
    )
    _register_type(
        monkeypatch, "g.type", max_attempts=3,
        steps=(StepSpec(step_key="wipe", tool_name="g.tool"),),
    )
    execution = _make(db_session, execution_type="g.type")
    enqueue(db_session, execution.id)
    step = grant_confirmation(
        db_session, execution.id, "wipe",
        actor_type="user", actor_id="alice", clock=_clock(),
    )
    assert step.confirmation_status == ConfirmationStatus.GRANTED.value
    assert step.confirmation_actor_id == "alice"
    assert step.confirmed_at is not None
    assert "CONFIRMATION_GRANTED" in _event_types(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    stats = worker.run_once()
    db_session.refresh(execution)
    assert stats["succeeded"] == 1
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert calls["n"] == 1
    db_session.refresh(step)
    assert step.status == StepStatus.SUCCEEDED.value
    assert step.confirmation_status == ConfirmationStatus.GRANTED.value


def test_reject_is_stable_failure(db_session, monkeypatch):
    calls = {"n": 0}

    def _danger(payload):
        calls["n"] += 1
        return {"done": True}

    _register_tool(
        monkeypatch, "r.tool", handler=_danger,
        destructive=True, requires_confirmation=True, replay_safe=False,
    )
    _register_type(
        monkeypatch, "r.type", max_attempts=3,
        steps=(StepSpec(step_key="wipe", tool_name="r.tool"),),
    )
    execution = _make(db_session, execution_type="r.type")
    enqueue(db_session, execution.id)
    reject_confirmation(
        db_session, execution.id, "wipe",
        actor_type="user", actor_id="bob", clock=_clock(),
    )
    assert "CONFIRMATION_REJECTED" in _event_types(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    stats = worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value  # 稳定失败，不重试
    assert stats["failed"] == 1
    assert calls["n"] == 0
    assert execution.last_error_code == "EXEC_CONFIRMATION_REJECTED"
    assert "RETRY_SCHEDULED" not in _event_types(db_session, execution.id)
    step = _step_by_key(db_session, execution.id, "wipe")
    assert step.confirmation_status == ConfirmationStatus.REJECTED.value
    assert step.confirmation_actor_id == "bob"


def test_grant_binds_input_hash_expired_on_drift(db_session, monkeypatch):
    """输入 hash 变化 → 旧确认立即失效（EXPIRED），绝不跨 hash 复用。"""
    calls = {"n": 0}

    def _danger(payload):
        calls["n"] += 1
        return {"done": True}

    _register_tool(
        monkeypatch, "drift.tool", handler=_danger,
        destructive=True, requires_confirmation=True, replay_safe=False,
    )

    def _with_literal(amount):
        _register_type(
            monkeypatch, "drift.type", max_attempts=3,
            steps=(
                StepSpec(step_key="pay", tool_name="drift.tool",
                         input_source="literal", input_literal={"amount": amount}),
            ),
        )

    _with_literal(100)
    execution = _make(db_session, execution_type="drift.type")
    enqueue(db_session, execution.id)
    step = grant_confirmation(
        db_session, execution.id, "pay",
        actor_type="user", actor_id="alice", clock=_clock(),
    )
    assert step.input_hash == _hash_of({"amount": 100})
    # 注册表漂移：金额变更（模拟模板升级）——旧确认绑定失效
    _with_literal(200)
    clock = FakeClock()
    worker = ExecutionWorker(
        _session_factory(db_session), worker_id="w1", clock=clock
    )
    stats = worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.QUEUED.value  # 可重试：等待重新确认
    assert stats["retried"] == 1
    assert calls["n"] == 0  # 失效后绝不执行
    db_session.refresh(step)
    assert step.confirmation_status == ConfirmationStatus.EXPIRED.value
    assert step.input_hash == _hash_of({"amount": 200})  # 指纹已同步
    # 规格固定 17 种事件类型（无 CONFIRMATION_EXPIRED）：失效由确认门的
    # STEP_FAILED 记录，错误码即 EXEC_CONFIRMATION_EXPIRED
    types = _event_types(db_session, execution.id)
    step_failed = next(
        e for e in list_events(db_session, execution.id)
        if e.event_type == "STEP_FAILED"
    )
    assert json.loads(step_failed.payload)["error_code"] == "EXEC_CONFIRMATION_EXPIRED"
    # 重新确认（绑定新 hash）→ 放行
    clock.advance(1)  # retry_after 到期
    grant_confirmation(
        db_session, execution.id, "pay",
        actor_type="user", actor_id="alice", clock=clock,
    )
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert calls["n"] == 1


def test_no_implicit_confirmation_and_explicit_actor(db_session, monkeypatch):
    """非 destructive 步骤 requires_confirmation=False；确认绝不伪造默认 actor。"""
    _register_tool(monkeypatch, "n.tool", handler=lambda p: {"ok": True})
    _register_type(
        monkeypatch, "n.type", max_attempts=1,
        steps=(StepSpec(step_key="s", tool_name="n.tool"),),
    )
    execution = _make(db_session, execution_type="n.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    step = _step_by_key(db_session, execution.id, "s")
    assert step.requires_confirmation is False
    assert step.confirmation_status is None  # 非 destructive 不设 PENDING
    # actor 是签名强制参数：缺失即 TypeError（绝不伪造默认确认）
    queued = _make(db_session, execution_type="n.type")
    enqueue(db_session, queued.id)
    with pytest.raises(TypeError):
        grant_confirmation(db_session, queued.id, "s", actor_type="user")


def test_confirm_only_in_confirmable_states(db_session, monkeypatch):
    _register_tool(
        monkeypatch, "c.tool", handler=lambda p: {"ok": True},
        destructive=True, requires_confirmation=True, replay_safe=False,
    )
    _register_type(
        monkeypatch, "c.type", max_attempts=3,
        steps=(StepSpec(step_key="s", tool_name="c.tool"),),
    )
    # CREATED：未入队不可确认
    created = _make(db_session, execution_type="c.type")
    with pytest.raises(InvalidExecutionTransitionError):
        grant_confirmation(
            db_session, created.id, "s",
            actor_type="user", actor_id="alice", clock=_clock(),
        )
    # 终态不可确认
    done = _make(db_session, execution_type="c.type")
    enqueue(db_session, done.id)
    transition_to(db_session, done.id, ExecutionStatus.CLAIMED, clock=_clock())
    transition_to(db_session, done.id, ExecutionStatus.RUNNING, clock=_clock())
    transition_to(db_session, done.id, ExecutionStatus.SUCCEEDED, clock=_clock())
    with pytest.raises(InvalidExecutionTransitionError):
        reject_confirmation(
            db_session, done.id, "s",
            actor_type="user", actor_id="alice", clock=_clock(),
        )


def test_confirmation_materializes_step_before_worker(db_session, monkeypatch):
    """grant 可先于任何 attempt 物化步骤（attempt_id=None），随后照常执行。"""
    _register_tool(
        monkeypatch, "m.tool", handler=lambda p: {"ok": True},
        destructive=True, requires_confirmation=True, replay_safe=False,
    )
    _register_type(
        monkeypatch, "m.type", max_attempts=3,
        steps=(StepSpec(step_key="s", tool_name="m.tool"),),
    )
    execution = _make(db_session, execution_type="m.type")
    enqueue(db_session, execution.id)
    step = grant_confirmation(
        db_session, execution.id, "s",
        actor_type="user", actor_id="alice", clock=_clock(),
    )
    assert step.attempt_id is None  # 未运行过
    assert step.status == StepStatus.PENDING.value
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    db_session.refresh(step)
    assert step.attempt_id is not None  # 执行时绑定 attempt
    assert step.status == StepStatus.SUCCEEDED.value


# =====================================================================
# 7. 安全恢复与幂等：SUCCEEDED 跳过 / RUNNING 重放裁决 / stale 终止
# =====================================================================


def test_succeeded_steps_never_rerun_on_retry(db_session, monkeypatch):
    """重试时 SUCCEEDED 步骤无条件跳过——绝不重跑、绝不覆写历史。"""
    counts = {"a": 0, "b": 0, "c": 0}

    def _mk(name, flaky=False):
        def _h(payload):
            counts[name] += 1
            if flaky and counts[name] == 1:
                raise RuntimeError("transient")
            return {"step": name}

        return _h

    _register_tool(monkeypatch, "skip.a", handler=_mk("a"))
    _register_tool(monkeypatch, "skip.b", handler=_mk("b", flaky=True))
    _register_tool(monkeypatch, "skip.c", handler=_mk("c"))
    _register_type(
        monkeypatch, "skip.type", max_attempts=3, base=0.001,
        steps=(
            StepSpec(step_key="s1", tool_name="skip.a"),
            StepSpec(step_key="s2", tool_name="skip.b"),
            StepSpec(step_key="s3", tool_name="skip.c"),
        ),
    )
    execution = _make(db_session, execution_type="skip.type")
    enqueue(db_session, execution.id)
    clock = FakeClock()
    worker = ExecutionWorker(
        _session_factory(db_session), worker_id="w1", clock=clock
    )
    worker.run_once()  # attempt 1: a 成功, b 失败
    clock.advance(1)
    worker.run_once()  # attempt 2: a 跳过, b 重跑成功, c 成功
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert counts == {"a": 1, "b": 2, "c": 1}  # a 只跑一次
    step_started = [
        e for e in list_events(db_session, execution.id)
        if e.event_type == "STEP_STARTED"
    ]
    keys = [json.loads(e.payload)["step"] for e in step_started]
    assert keys == ["s1", "s2", "s2", "s3"]  # 跳过无新事件
    # s1 的输出从未被覆写（attempt 1 的结果保留）
    s1 = _step_by_key(db_session, execution.id, "s1")
    assert json.loads(s1.output_json) == {"step": "a"}
    attempt1 = (
        db_session.query(ExecutionAttempt)
        .filter(
            ExecutionAttempt.execution_id == execution.id,
            ExecutionAttempt.attempt_number == 1,
        )
        .one()
    )
    assert s1.attempt_id == attempt1.id


def test_crashed_running_replay_safe_recovers(db_session, monkeypatch):
    """Worker 崩溃后 RUNNING 步骤：replay_safe 工具恢复执行（重放）。"""
    calls = {"n": 0}

    def _h(payload):
        calls["n"] += 1
        return {"pong": True}

    _register_tool(monkeypatch, "cr.tool", handler=_h)
    _register_type(
        monkeypatch, "cr.type", max_attempts=3,
        steps=(StepSpec(step_key="s", tool_name="cr.tool"),),
    )
    execution = _make(db_session, execution_type="cr.type")
    enqueue(db_session, execution.id)
    clock = FakeClock()
    claimed = _claim(db_session, worker_id="crashy", clock=clock)
    execution, attempt = claimed
    transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
    # 崩溃模拟：步骤已进入 RUNNING（Worker 死在 handler 调用前后）
    now = clock.now()
    db_session.add(
        ExecutionStep(
            execution_id=execution.id, attempt_id=attempt.id,
            step_key="s", step_index=0, tool_name="cr.tool",
            status=StepStatus.RUNNING.value,
            input_json="{}", input_hash=_hash_of({}),
            requires_confirmation=False,
            created_at=now, updated_at=now,
        )
    )
    db_session.commit()
    clock.advance(61)  # 租约过期
    recover_expired(db_session, clock=clock)  # attempt 1 ABORTED，execution 回队
    assert "LEASE_RECOVERED" in _event_types(db_session, execution.id)
    worker = ExecutionWorker(
        _session_factory(db_session), worker_id="w2", clock=clock
    )
    stats = worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert stats["succeeded"] == 1
    assert calls["n"] == 1  # 重放恰好一次
    step = _step_by_key(db_session, execution.id, "s")
    assert step.status == StepStatus.SUCCEEDED.value
    types = _event_types(db_session, execution.id)
    # 崩溃发生在 STEP_STARTED 之前（模拟未写事件），恢复重放恰好一次
    assert types.count("STEP_STARTED") == 1


def test_crashed_running_replay_unsafe_stable_failure(db_session, monkeypatch):
    """replay_safe=False（含全部 destructive）崩溃后绝不自动重放。"""
    calls = {"n": 0}

    def _side_effect(payload):
        calls["n"] += 1
        return {"done": True}

    _register_tool(
        monkeypatch, "unsafe.tool", handler=_side_effect,
        destructive=False, requires_confirmation=False,
        replay_safe=False, retryable=False,
    )
    _register_type(
        monkeypatch, "unsafe.type", max_attempts=3,
        steps=(StepSpec(step_key="s", tool_name="unsafe.tool"),),
    )
    execution = _make(db_session, execution_type="unsafe.type")
    enqueue(db_session, execution.id)
    clock = FakeClock()
    claimed = _claim(db_session, worker_id="crashy", clock=clock)
    execution, attempt = claimed
    transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
    db_session.add(
        ExecutionStep(
            execution_id=execution.id, attempt_id=attempt.id,
            step_key="s", step_index=0, tool_name="unsafe.tool",
            status=StepStatus.RUNNING.value,
            input_json="{}", input_hash=_hash_of({}),
            requires_confirmation=False,
            created_at=clock.now(), updated_at=clock.now(),
        )
    )
    db_session.commit()
    clock.advance(61)
    recover_expired(db_session, clock=clock)
    worker = ExecutionWorker(
        _session_factory(db_session), worker_id="w2", clock=clock
    )
    stats = worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value  # 稳定失败
    assert stats["failed"] == 1
    assert calls["n"] == 0  # 绝不重放
    assert execution.last_error_code == "EXEC_STEP_REPLAY_UNSAFE"
    step = _step_by_key(db_session, execution.id, "s")
    assert step.status == StepStatus.FAILED.value
    assert step.error_code == "EXEC_STEP_REPLAY_UNSAFE"
    assert "RETRY_SCHEDULED" not in _event_types(db_session, execution.id)


def test_stale_worker_blocks_at_first_write(db_session):
    """租约被回收后，stale worker 的编排在第一次写入即 EXEC_CLAIM_LOST。"""
    execution = _make(db_session)
    enqueue(db_session, execution.id)
    clock = FakeClock()
    claimed = _claim(db_session, worker_id="stale-w", clock=clock)
    execution, attempt = claimed
    transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
    clock.advance(61)
    recover_expired(db_session, clock=clock)  # 租约被回收（attempt ABORTED）
    events_after_recover = len(list_events(db_session, execution.id))
    with pytest.raises(ExecutionClaimLostError):
        run_execution_steps(db_session, execution, attempt, clock=clock)
    # stale worker 未写任何 step / 事件
    assert (
        db_session.query(ExecutionStep)
        .filter(ExecutionStep.execution_id == execution.id)
        .count()
        == 0
    )
    assert len(list_events(db_session, execution.id)) == events_after_recover


def test_finished_attempt_cannot_write(db_session):
    """attempt 已终态 + 新 attempt 生效后，旧 attempt 的任何写入被租约闸门拒绝。"""
    execution = _make(db_session)
    execution, attempt = _start_claimed(db_session, execution.id)
    run_execution_steps(db_session, execution, attempt, clock=_clock())
    # 执行完成但 execution 尚未终态（模拟：旧 worker 又写了一个 step）
    transition_to(db_session, execution.id, ExecutionStatus.SUCCEEDED, clock=_clock())
    with pytest.raises(StepInvalidError):  # 终态拒绝先于一切
        run_execution_steps(db_session, execution, attempt, clock=_clock())


# =====================================================================
# 8. 敏感输入 fail-closed + 输出/错误脱敏 + 截断存储
# =====================================================================


def test_secret_input_never_persisted(db_session, monkeypatch):
    """嵌套敏感字段名 → 任何落盘之前拒绝：不建步骤行、不写输入、不执行。"""
    calls = {"n": 0}

    def _h(payload):
        calls["n"] += 1
        return {"ok": True}

    _register_tool(monkeypatch, "sec.in", handler=_h)
    _register_type(
        monkeypatch, "sec.in.type", max_attempts=1,
        steps=(
            StepSpec(step_key="s", tool_name="sec.in",
                     input_source="literal",
                     input_literal={"settings": {"api_key": "sk-topsecret-999"}}),
        ),
    )
    execution = _make(db_session, execution_type="sec.in.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.last_error_code == "EXEC_SCHEMA_INVALID"
    assert calls["n"] == 0
    assert (
        db_session.query(ExecutionStep)
        .filter(ExecutionStep.execution_id == execution.id)
        .count()
        == 0  # 步骤行从未物化——敏感内容绝不入库存证
    )
    for ev in list_events(db_session, execution.id):
        assert "sk-topsecret-999" not in (ev.payload or "")
    # 确认动作同样拒绝绑定敏感输入
    execution2 = _make(db_session, execution_type="sec.in.type")
    enqueue(db_session, execution2.id)
    with pytest.raises(StepSchemaError):
        grant_confirmation(
            db_session, execution2.id, "s",
            actor_type="user", actor_id="alice", clock=_clock(),
        )
    db_session.rollback()


def test_output_and_error_sanitized(db_session, monkeypatch):
    def _leaky(payload):
        return {"api_key": "sk-out-777", "nested": {"token": "tok-8"}}

    _register_tool(monkeypatch, "leak.tool", handler=_leaky)
    _register_type(
        monkeypatch, "leak.type", max_attempts=1,
        steps=(StepSpec(step_key="s", tool_name="leak.tool"),),
    )
    execution = _make(db_session, execution_type="leak.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    step = _step_by_key(db_session, execution.id, "s")
    out = json.loads(step.output_json)
    assert out == {
        "<redacted>": "<redacted>",
        "nested": {"<redacted>": "<redacted>"},
    }
    assert "sk-out-777" not in step.output_json
    assert "tok-8" not in step.output_json

    # 错误路径：超长消息截断 + 无 traceback / 无异常类名
    def _angry(payload):
        raise RuntimeError("boom-" + "x" * 2000)

    _register_tool(monkeypatch, "angry.tool", handler=_angry)
    _register_type(
        monkeypatch, "angry.type", max_attempts=1,
        steps=(StepSpec(step_key="s", tool_name="angry.tool"),),
    )
    bad = _make(db_session, execution_type="angry.type")
    enqueue(db_session, bad.id)
    worker.run_once()
    db_session.refresh(bad)
    assert bad.status == ExecutionStatus.FAILED.value
    assert bad.last_error_code == "EXEC_HANDLER_ERROR"
    bstep = _step_by_key(db_session, bad.id, "s")
    assert len(bstep.error_message) <= 500
    assert "Traceback" not in bstep.error_message
    assert "RuntimeError" not in bstep.error_message
    assert "at 0x" not in bstep.error_message


def test_input_storage_truncated_but_full_input_used(db_session, monkeypatch):
    received = {}

    def _h(payload):
        received["input"] = payload
        return {"ok": True}

    _register_tool(monkeypatch, "big.tool", handler=_h)
    big = "x" * 3000
    _register_type(
        monkeypatch, "big.type", max_attempts=1,
        steps=(
            StepSpec(step_key="s", tool_name="big.tool",
                     input_source="literal", input_literal={"v": big}),
        ),
    )
    execution = _make(db_session, execution_type="big.type")
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert received["input"] == {"v": big}  # handler 收到完整输入
    step = _step_by_key(db_session, execution.id, "s")
    assert len(step.input_json) == STEP_INPUT_MAX_CHARS  # 落盘截断
    assert "x" * 3000 not in step.input_json  # 完整内容绝不落盘
    # 完整输入参与 hash 绑定（截断不破坏指纹）
    assert step.input_hash == _hash_of({"v": big})


# =====================================================================
# 9. 表约束：双 UNIQUE / FK CASCADE / SET NULL / 事件序列 UNIQUE
# =====================================================================


def test_step_and_event_table_constraints(db_session):
    execution = _make(db_session)
    execution, attempt = _start_claimed(db_session, execution.id)
    now = FIXED_NOW
    step = ExecutionStep(
        execution_id=execution.id, attempt_id=attempt.id,
        step_key="ping", step_index=0, tool_name="system.ping",
        status=StepStatus.SUCCEEDED.value,
        input_json="{}", input_hash=_hash_of({}),
        requires_confirmation=False, created_at=now, updated_at=now,
    )
    db_session.add(step)
    db_session.commit()
    # UNIQUE(execution_id, step_index)
    dup_index = ExecutionStep(
        execution_id=execution.id, attempt_id=None,
        step_key="other", step_index=0, tool_name="system.ping",
        status=StepStatus.PENDING.value,
        input_json="{}", input_hash=_hash_of({}),
        requires_confirmation=False, created_at=now, updated_at=now,
    )
    db_session.add(dup_index)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    # UNIQUE(execution_id, step_key)
    dup_key = ExecutionStep(
        execution_id=execution.id, attempt_id=None,
        step_key="ping", step_index=1, tool_name="system.ping",
        status=StepStatus.PENDING.value,
        input_json="{}", input_hash=_hash_of({}),
        requires_confirmation=False, created_at=now, updated_at=now,
    )
    db_session.add(dup_key)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    # 事件序列 UNIQUE（DB 级审计约束）
    ev_indexes = {
        idx["name"]: idx["unique"]
        for idx in inspect(db_session.get_bind()).get_indexes("execution_events")
    }
    assert ev_indexes.get("uq_execution_events_execution_sequence")


def test_fk_cascade_and_set_null(db_session):
    execution = _make(db_session)
    execution, attempt = _start_claimed(db_session, execution.id)
    run_execution_steps(db_session, execution, attempt, clock=_clock())
    step_id = _step_by_key(db_session, execution.id, "ping").id
    # attempt 行删除 → steps/events 的 attempt_id SET NULL（审计存活）
    db_session.query(ExecutionAttempt).filter(
        ExecutionAttempt.id == attempt.id
    ).delete()
    db_session.commit()
    db_session.expire_all()
    assert db_session.get(ExecutionStep, step_id).attempt_id is None
    # execution 删除 → steps/events 级联删除
    db_session.delete(execution)
    db_session.commit()
    assert db_session.query(ExecutionStep).count() == 0
    assert db_session.query(ExecutionEvent).count() == 0


# =====================================================================
# 10. 迁移：SQLite 回环 / PG 离线 SQL / 单 head 链
# =====================================================================


def test_migration_sqlite_roundtrip_6c(tmp_path):
    db_file = tmp_path / "migrate6c.db"
    cfg = _make_config(db_file)
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "execution_steps" in tables
    assert "execution_events" in tables
    step_cols = {
        col["name"] for col in inspector.get_columns("execution_steps")
    }
    assert {
        "execution_id", "attempt_id", "step_key", "step_index", "tool_name",
        "status", "input_json", "input_hash", "output_json", "error_code",
        "error_message", "requires_confirmation", "confirmation_status",
        "confirmation_actor_type", "confirmation_actor_id", "confirmed_at",
    } <= step_cols
    step_indexes = {
        idx["name"]: idx["unique"]
        for idx in inspector.get_indexes("execution_steps")
    }
    assert step_indexes.get("uq_execution_steps_execution_index")
    assert step_indexes.get("uq_execution_steps_execution_key")
    ev_indexes = {
        idx["name"]: idx["unique"]
        for idx in inspector.get_indexes("execution_events")
    }
    assert ev_indexes.get("uq_execution_events_execution_sequence")
    step_fks = inspector.get_foreign_keys("execution_steps")
    assert any(
        fk["referred_table"] == "executions"
        and "CASCADE" in str(fk.get("options", {}))
        for fk in step_fks
    )
    assert any(
        fk["referred_table"] == "execution_attempts"
        and "SET NULL" in str(fk.get("options", {}))
        for fk in step_fks
    )
    engine.dispose()
    # downgrade：只移除 6C 新增对象，既有表保留
    command.downgrade(cfg, "7b8c9d0e1f2a3")
    engine = create_engine(f"sqlite:///{db_file}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "execution_steps" not in tables
    assert "execution_events" not in tables
    assert "execution_attempts" in tables
    assert "executions" in tables
    assert "tasks" in tables
    engine.dispose()
    # re-upgrade 恢复
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    assert "execution_steps" in set(inspect(engine).get_table_names())
    con = sqlite3.connect(db_file)
    assert (
        con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        == "8c9d0e1f2a3b4"
    )
    con.close()
    engine.dispose()


def test_pg_offline_sql_generation_6c(capsys):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:pass@localhost:5432/jarvis_pg_check",
    )
    command.upgrade(cfg, "head", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE execution_steps" in sql
    assert "CREATE TABLE execution_events" in sql
    assert "CREATE UNIQUE INDEX uq_execution_steps_execution_index" in sql
    assert "CREATE UNIQUE INDEX uq_execution_steps_execution_key" in sql
    assert "CREATE UNIQUE INDEX uq_execution_events_execution_sequence" in sql
    assert "ON DELETE CASCADE" in sql
    assert "ON DELETE SET NULL" in sql


def test_single_head_and_chain_6c():
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    heads = set(ScriptDirectory.from_config(cfg).get_heads())
    assert heads == {"8c9d0e1f2a3b4"}
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
