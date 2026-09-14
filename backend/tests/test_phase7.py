"""Phase 7.1 专项测试：Execution 域真实业务工具注册。

覆盖矩阵（按授权清单）：
A. Registry：
   - 类型/工具解析（reminder.send / documents.reindex / system.ping 共存）；
   - 未知类型/工具 fail-closed（EXEC_TYPE_UNKNOWN / EXEC_TOOL_UNKNOWN）；
   - 未知 payload 字段拒绝（EXEC_PAYLOAD_INVALID，绝不静默丢弃）；
   - 缺失字段/类型错误拒绝（pydantic strict + extra="forbid"）；
   - ToolSpec 构造期安全约束（destructive 必须 requires_confirmation、
     destructive 必须 replay_safe=False，违规注册直接 ValueError）；
   - handler 静态引用（模块级白名单，无字符串 import/eval/exec/反射；
     源码扫描断言）。
B. reminder.send：
   - 成功恰好创建一条对应应用内 Notification（type=REMINDER_DUE、
     title/body 与 Reminder Worker 一致）；
   - 重放不产生重复 Notification（唯一约束幂等）；
   - 不同 executions 按业务语义各投递各的 reminder；
   - handler 失败 → step/event/execution 正确失败状态
     （EXEC_REMINDER_NOT_FOUND / DELIVERY_FAILED / DELIVERY_RETRYABLE）；
   - FakeClock 注入投递时间戳（无真实 sleep / 无外部通道）。
C. documents.reindex：
   - 无 GRANTED 确认绝不执行（EXEC_CONFIRMATION_REQUIRED，spy 零调用）；
   - GRANTED → 恰好一次（spy 计数 + 真实索引结果 INDEXED）；
   - REJECTED → 绝不执行（EXEC_CONFIRMATION_REJECTED，稳定失败）；
   - EXPIRED（payload 漂移致确认 hash 绑定失效）→ 绝不执行；
   - 确认 ownership 隔离（对别的 execution 的确认不生效）；
   - replay-unsafe 规则（崩溃后 RUNNING 步骤绝不重放）；
   - stale worker 不写 step/event/terminal 状态（EXEC_CLAIM_LOST）；
   - spy 证明真实 RetrievalIndexService.reindex_document 被调用
     （非 test double 绕过业务路径）。
D. 回归：6A-6F 定向 / 全量 / collect-only 在验证阶段命令级执行。

基础设施约定（沿 Phase 6C）：
- 每个测试独立 tmp_path SQLite（FK pragma）；worker 用同库独立
  session 工厂模拟独立进程；
- handler 的数据库与时钟经模块级注入点接入测试库：
  monkeypatch.setattr(execution_tools.reminder_send, "_session_factory", ...)
  / "_clock"；生产默认仍是 app.core.database.SessionLocal / SystemClock；
- 全部离线：local-hash embedding（零网络）、无真实 sleep。
"""

import hashlib
import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.database import Base, _set_sqlite_pragma
from app.models.document import (
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentIndexStatus,
    DocumentStatus,
)
from app.models.execution import Execution, ExecutionStatus
from app.models.execution_step import (
    ConfirmationStatus,
    ExecutionStep,
    StepStatus,
)
from app.models.reminder import (
    Notification,
    NotificationType,
    Reminder,
    ReminderStatus,
)
from app.models.task import Task, TaskStatus
from app.services import document_service
from app.services.execution_claim import claim_next, recover_expired
from app.services.execution_errors import (
    ExecutionClaimLostError,
    ExecutionPayloadError,
    ExecutionTypeUnknownError,
    ToolUnknownError,
)
from app.services.execution_events import list_events
from app.services.execution_registry import (
    EXECUTION_TOOLS,
    EXECUTION_TYPES,
    StepSpec,
    ToolSpec,
    get_tool,
    get_type_spec,
    normalize_payload,
)
from app.services.execution_service import create_execution, enqueue, transition_to
from app.services.execution_steps import (
    _hash_of,
    grant_confirmation,
    reject_confirmation,
    run_execution_steps,
)
from app.services.execution_tools import document_reindex as document_reindex_mod
from app.services.execution_tools import reminder_send as reminder_send_mod
from app.services.execution_worker import ExecutionWorker
from app.services.retrieval_index_service import RetrievalIndexService

BACKEND_DIR = Path(__file__).resolve().parent.parent
FIXED_NOW = datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc)
REMINDER_PAYLOAD = {"reminder_id": 1, "task_id": 1, "message": "due now"}
REINDEX_PAYLOAD = {"document_id": 1}


class FakeClock:
    """可推进时钟（6C 同款）：模拟 lease 过期 / backoff 时间流逝。"""

    def __init__(self, start=FIXED_NOW):
        self._now = start

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)


def _clock():
    return FakeClock(FIXED_NOW)


def _as_utc(value: datetime | None) -> datetime | None:
    """SQLite 读回的 naive datetime 统一视为 UTC（与 _to_db_utc 语义一致）。"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


_key_counter = itertools.count(1)


def _unique_key() -> str:
    return f"test-7-key-{next(_key_counter):08d}"


@pytest.fixture()
def db_session(tmp_path):
    db_file = tmp_path / "test_7.db"
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


def _make(db, key=None, payload=None, execution_type="reminder.send", **kwargs):
    return create_execution(
        db,
        execution_type=execution_type,
        payload=payload or (REMINDER_PAYLOAD if execution_type == "reminder.send" else REINDEX_PAYLOAD),
        idempotency_key=key or _unique_key(),
        clock=_clock(),
        **kwargs,
    )


def _event_types(db, execution_id):
    return [e.event_type for e in list_events(db, execution_id)]


def _step_by_key(db, execution_id, step_key):
    return (
        db.query(ExecutionStep)
        .filter(
            ExecutionStep.execution_id == execution_id,
            ExecutionStep.step_key == step_key,
        )
        .one()
    )


def _notifications(db):
    return db.query(Notification).order_by(Notification.id.asc()).all()


def _make_task(db, title="Buy milk"):
    task = Task(title=title, status=TaskStatus.CONFIRMED.value)
    db.add(task)
    db.commit()
    return task


def _make_reminder(db, task, remind_at=None, status=ReminderStatus.PENDING.value):
    reminder = Reminder(
        task_id=task.id,
        status=status,
        remind_at=remind_at or (FIXED_NOW - timedelta(minutes=1)),
    )
    db.add(reminder)
    db.commit()
    return reminder


def _make_ready_document(db, content=b"phase7 document body text", name="p7.txt"):
    doc = document_service.upload_and_process(
        db, filename=name, content_type="text/plain", content=content
    )
    assert doc.status == DocumentStatus.READY.value
    db.refresh(doc)
    return doc


def _inject_reminder(db_session, monkeypatch, clock=None):
    """handler 注入点：测试库 session 工厂 + 固定时钟（生产默认为
    app.core.database.SessionLocal / SystemClock，本函数仅测试替换）。"""
    monkeypatch.setattr(
        reminder_send_mod, "_session_factory", _session_factory(db_session)
    )
    clock = clock or _clock()
    monkeypatch.setattr(reminder_send_mod, "_clock", clock)
    return clock


def _inject_reindex(db_session, monkeypatch):
    """handler 注入点（同 reminder.send）：测试库 session 工厂。
    返回测试时钟（生产默认为 SessionLocal / 系统时钟）。"""
    monkeypatch.setattr(
        document_reindex_mod, "_session_factory", _session_factory(db_session)
    )
    return FakeClock()


def _spy_reindex(monkeypatch):
    """包装真实 reindex_document 的 spy：普通函数（保留描述符绑定，
    避免 mock.Mock 作为类属性时丢失 self），调用真实实现并计数。"""
    real = RetrievalIndexService.reindex_document
    state = {"n": 0, "args": []}

    def _spy(self, document_id):
        state["n"] += 1
        state["args"].append(document_id)
        return real(self, document_id)

    monkeypatch.setattr(RetrievalIndexService, "reindex_document", _spy)
    return state


# =====================================================================
# A. Registry：类型解析 / fail-closed / 构造期约束 / 静态引用
# =====================================================================


def test_types_and_tools_registered():
    """三个类型共存；新工具声明字段逐项正确（含构造期强制组合）。"""
    assert set(EXECUTION_TYPES) == {"system.ping", "reminder.send", "documents.reindex"}
    assert set(EXECUTION_TOOLS) == {"system.ping", "reminder.send", "documents.reindex"}

    send = EXECUTION_TOOLS["reminder.send"]
    assert send.destructive is False
    assert send.requires_confirmation is False
    assert send.retryable is True
    assert send.replay_safe is True  # 非破坏可重放
    assert send.sanitize_result is True
    assert send.enabled is True
    assert send.input_schema.model_config.get("extra") == "forbid"
    assert send.output_schema.model_config.get("extra") == "forbid"

    reindex = EXECUTION_TOOLS["documents.reindex"]
    assert reindex.destructive is True
    assert reindex.requires_confirmation is True  # destructive 强制确认
    assert reindex.replay_safe is False  # destructive 强制不可重放
    assert reindex.retryable is True

    send_type = EXECUTION_TYPES["reminder.send"]
    assert send_type.payload_allowed_fields == frozenset(
        {"reminder_id", "task_id", "message"}
    )
    assert send_type.steps == (StepSpec(step_key="send", tool_name="reminder.send"),)

    reindex_type = EXECUTION_TYPES["documents.reindex"]
    assert reindex_type.payload_allowed_fields == frozenset({"document_id"})
    assert reindex_type.steps == (
        StepSpec(step_key="reindex", tool_name="documents.reindex"),
    )


def test_unknown_type_and_tool_fail_closed():
    """未知类型/工具拒绝（绝不静默回退或合成）。"""
    with pytest.raises(ExecutionTypeUnknownError) as e1:
        get_type_spec("no.such.type")
    assert e1.value.code == "EXEC_TYPE_UNKNOWN"
    with pytest.raises(ToolUnknownError) as e2:
        get_tool("no.such.tool", legacy_fallback=False)
    assert e2.value.code == "EXEC_TOOL_UNKNOWN"


def test_payload_unknown_fields_rejected():
    """白名单外字段拒绝（fail-closed，绝不静默丢弃）。"""
    with pytest.raises(ExecutionPayloadError) as e1:
        normalize_payload(
            "reminder.send",
            {"reminder_id": 1, "task_id": 1, "message": "x", "evil": True},
        )
    assert e1.value.code == "EXEC_PAYLOAD_INVALID"
    with pytest.raises(ExecutionPayloadError) as e2:
        normalize_payload(
            "documents.reindex", {"document_id": 1, "nuke_all": True}
        )
    assert e2.value.code == "EXEC_PAYLOAD_INVALID"
    # 敏感字段名同样拒绝（registry deny 词表第二层）
    with pytest.raises(ExecutionPayloadError):
        normalize_payload("reminder.send", {"reminder_id": 1, "api_key": "k"})


def test_payload_missing_and_type_errors_rejected():
    """非对象 payload / 缺失字段 / 类型错误 → 拒绝（strict schema）。"""
    with pytest.raises(ExecutionPayloadError):
        normalize_payload("reminder.send", ["not", "a", "dict"])
    # normalize 只查字段名；类型错误由工具 schema strict 校验拦截
    normalized, _ = normalize_payload(
        "reminder.send", {"reminder_id": "abc", "task_id": 1, "message": "x"}
    )
    with pytest.raises(Exception) as e1:
        EXECUTION_TOOLS["reminder.send"].input_schema.model_validate(
            normalized, strict=True
        )
    assert "reminder_id" in str(e1.value)
    # 缺失字段（白名单字段未提供）→ schema 拒绝
    with pytest.raises(Exception) as e2:
        EXECUTION_TOOLS["reminder.send"].input_schema.model_validate(
            {"reminder_id": 1}, strict=True
        )
    assert "task_id" in str(e2.value)
    with pytest.raises(Exception) as e3:
        EXECUTION_TOOLS["documents.reindex"].input_schema.model_validate(
            {}, strict=True
        )
    assert "document_id" in str(e3.value)


def test_toolspec_construction_safety_constraints():
    """构造期安全约束：违规注册直接 ValueError（fail-closed，绝不静默修正）。"""
    with pytest.raises(ValueError):
        ToolSpec(
            name="bad.1", destructive=True, requires_confirmation=False,
            replay_safe=False,
        )
    with pytest.raises(ValueError):
        ToolSpec(
            name="bad.2", destructive=True, requires_confirmation=True,
            replay_safe=True,
        )
    # 注册表内组合必须已满足约束（上面 test 已逐字段断言）


def test_handlers_statically_referenced_no_dynamic_paths():
    """handler 是白名单静态模块引用；handler 源码无动态代码路径
    （无字符串 import / eval / exec / importlib / __import__ / getattr
    反射 / sleep / 外部通道）。"""
    assert EXECUTION_TOOLS["reminder.send"].handler.__module__ == (
        "app.services.execution_tools.reminder_send"
    )
    assert EXECUTION_TOOLS["documents.reindex"].handler.__module__ == (
        "app.services.execution_tools.document_reindex"
    )
    forbidden = [
        "__import__", "importlib", "eval(", "exec(", "compile(",
        "getattr(", "setattr(", "globals()[", "locals()[",
        "time.sleep", "sleep(", "smtplib", "smtp", "requests.",
        "httpx.", "urllib", "socket", "wechat", "weixin", "subprocess",
        "os.system", "Popen",
    ]
    for mod in (
        reminder_send_mod.__file__,
        document_reindex_mod.__file__,
    ):
        src = Path(mod).read_text(encoding="utf-8")
        assert "import " in src  # 静态 import 允许（白名单依赖）
        for token in forbidden:
            assert token not in src, f"{Path(mod).name} contains {token!r}"


# =====================================================================
# B. reminder.send：投递语义 / 幂等 / 失败状态
# =====================================================================


def test_reminder_send_success_single_notification(db_session, monkeypatch):
    """成功：恰好一条应用内 Notification（与 Reminder Worker 同构）、
    reminder DELIVERED、时间戳来自注入时钟。"""
    clock = _inject_reminder(db_session, monkeypatch)
    task = _make_task(db_session, title="Pay rent")
    reminder = _make_reminder(db_session, task)
    execution = _make(
        db_session,
        payload={"reminder_id": reminder.id, "task_id": task.id, "message": "due"},
    )
    enqueue(db_session, execution.id, clock=clock)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1", clock=clock)
    stats = worker.run_once()
    assert stats["succeeded"] == 1
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.SUCCEEDED.value

    notes = _notifications(db_session)
    assert len(notes) == 1  # 恰好一条
    note = notes[0]
    assert note.reminder_id == reminder.id
    assert note.task_id == task.id
    assert note.type == NotificationType.REMINDER_DUE.value
    assert note.title == "任务提醒：Pay rent"
    assert note.body == "任务「Pay rent」已到截止时间。"

    db_session.refresh(reminder)
    assert reminder.status == ReminderStatus.DELIVERED.value
    assert _as_utc(reminder.delivered_at) == FIXED_NOW  # 注入时钟生效

    step = _step_by_key(db_session, execution.id, "send")
    assert step.status == StepStatus.SUCCEEDED.value
    assert "delivered" in step.output_json
    assert '"message":"due"' in step.output_json  # message 回显（紧凑 JSON）
    assert "STEP_STARTED" in _event_types(db_session, execution.id)
    assert "STEP_SUCCEEDED" in _event_types(db_session, execution.id)
    assert "EXECUTION_SUCCEEDED" in _event_types(db_session, execution.id)


def test_reminder_send_replay_no_duplicate(db_session, monkeypatch):
    """重放：同一 reminder 二次执行（新 execution）不产生重复 Notification
    （_deliver 唯一约束幂等语义）。"""
    _inject_reminder(db_session, monkeypatch)
    task = _make_task(db_session)
    reminder = _make_reminder(db_session, task)
    for _ in range(2):
        execution = _make(
            db_session,
            payload={"reminder_id": reminder.id, "task_id": task.id, "message": "m"},
        )
        enqueue(db_session, execution.id)
        stats = ExecutionWorker(_session_factory(db_session), worker_id="w1").run_once()
        assert stats["succeeded"] == 1
    notes = _notifications(db_session)
    assert len(notes) == 1  # 唯一约束防重：始终一条
    db_session.refresh(reminder)
    assert reminder.status == ReminderStatus.DELIVERED.value


def test_reminder_send_different_executions_business_semantics(db_session, monkeypatch):
    """不同 executions：各投递各的 reminder（互不串扰）。"""
    _inject_reminder(db_session, monkeypatch)
    task_a = _make_task(db_session, title="Task A")
    task_b = _make_task(db_session, title="Task B")
    rem_a = _make_reminder(db_session, task_a)
    rem_b = _make_reminder(db_session, task_b)
    for task, reminder in ((task_a, rem_a), (task_b, rem_b)):
        execution = _make(
            db_session,
            payload={"reminder_id": reminder.id, "task_id": task.id, "message": "m"},
        )
        enqueue(db_session, execution.id)
        stats = ExecutionWorker(_session_factory(db_session), worker_id="w1").run_once()
        assert stats["succeeded"] == 1
    notes = _notifications(db_session)
    assert len(notes) == 2
    assert [n.reminder_id for n in notes] == [rem_a.id, rem_b.id]
    assert notes[0].title == "任务提醒：Task A"
    assert notes[1].title == "任务提醒：Task B"
    db_session.refresh(rem_a)
    db_session.refresh(rem_b)
    assert rem_a.status == ReminderStatus.DELIVERED.value
    assert rem_b.status == ReminderStatus.DELIVERED.value


def test_reminder_send_missing_reminder_fails(db_session, monkeypatch):
    """reminder 不存在 → step FAILED EXEC_REMINDER_NOT_FOUND；工具 retryable
    → 先 RETRY_SCHEDULED，重试耗尽 max_attempts 后 execution 终态 FAILED。"""
    clock = _inject_reminder(db_session, monkeypatch)
    task = _make_task(db_session)
    execution = _make(
        db_session, payload={"reminder_id": 9999, "task_id": task.id, "message": "m"}
    )
    enqueue(db_session, execution.id, clock=clock)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1", clock=clock)
    stats = worker.run_once()
    db_session.refresh(execution)
    assert stats["retried"] == 1  # retryable：等待重试（attempt < max_attempts）
    assert execution.status == ExecutionStatus.QUEUED.value
    step = _step_by_key(db_session, execution.id, "send")
    assert step.status == StepStatus.FAILED.value
    assert step.error_code == "EXEC_REMINDER_NOT_FOUND"
    assert "RETRY_SCHEDULED" in _event_types(db_session, execution.id)
    assert _notifications(db_session) == []  # 无副作用
    # 重试耗尽（max_attempts=3）→ 终态 FAILED，错误码保留
    for _ in range(2):
        clock.advance(3600)  # 越过 backoff / retry_after
        worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.last_error_code == "EXEC_REMINDER_NOT_FOUND"
    assert _notifications(db_session) == []


def test_reminder_send_task_mismatch_fails(db_session, monkeypatch):
    """task_id 与 reminder 归属不符 → 拒绝投递（绝不投给错误任务）。"""
    clock = _inject_reminder(db_session, monkeypatch)
    task = _make_task(db_session)
    other = _make_task(db_session, title="Other")
    reminder = _make_reminder(db_session, task)
    execution = _make(
        db_session,
        payload={"reminder_id": reminder.id, "task_id": other.id, "message": "m"},
    )
    enqueue(db_session, execution.id, clock=clock)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1", clock=clock)
    stats = worker.run_once()
    db_session.refresh(execution)
    assert stats["retried"] == 1  # retryable：先等待重试
    assert execution.status == ExecutionStatus.QUEUED.value
    step = _step_by_key(db_session, execution.id, "send")
    assert step.error_code == "EXEC_REMINDER_NOT_FOUND"
    assert "RETRY_SCHEDULED" in _event_types(db_session, execution.id)
    assert _notifications(db_session) == []
    db_session.refresh(reminder)
    assert reminder.status == ReminderStatus.PENDING.value  # 未被触碰
    # 耗尽重试 → 终态 FAILED
    for _ in range(2):
        clock.advance(3600)
        worker.run_once()
    db_session.refresh(execution)
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.last_error_code == "EXEC_REMINDER_NOT_FOUND"
    assert _notifications(db_session) == []
    db_session.refresh(reminder)
    assert reminder.status == ReminderStatus.PENDING.value


def test_reminder_send_delivery_failure_codes(db_session, monkeypatch):
    """_deliver 的失败分类 → 稳定错误码（重试裁决仍归执行域 retryable）。"""
    task = _make_task(db_session)
    reminder = _make_reminder(db_session, task)

    # 可重试数据库错误 → EXEC_REMINDER_DELIVERY_RETRYABLE（RETRY_SCHEDULED）
    monkeypatch.setattr(
        reminder_send_mod, "_deliver", lambda db, r, now: "claimed"
    )
    _inject_reminder(db_session, monkeypatch)
    ex1 = _make(
        db_session,
        payload={"reminder_id": reminder.id, "task_id": task.id, "message": "m"},
    )
    enqueue(db_session, ex1.id)
    stats1 = ExecutionWorker(_session_factory(db_session), worker_id="w1").run_once()
    db_session.refresh(ex1)
    assert stats1["retried"] == 1  # retryable：等待重试
    assert ex1.status == ExecutionStatus.QUEUED.value
    step1 = _step_by_key(db_session, ex1.id, "send")
    assert step1.status == StepStatus.FAILED.value
    assert step1.error_code == "EXEC_REMINDER_DELIVERY_RETRYABLE"
    assert "RETRY_SCHEDULED" in _event_types(db_session, ex1.id)

    # 永久数据错误 → EXEC_REMINDER_DELIVERY_FAILED
    monkeypatch.setattr(
        reminder_send_mod, "_deliver", lambda db, r, now: "failed"
    )
    ex2 = _make(
        db_session,
        payload={"reminder_id": reminder.id, "task_id": task.id, "message": "m"},
    )
    enqueue(db_session, ex2.id)
    stats2 = ExecutionWorker(_session_factory(db_session), worker_id="w1").run_once()
    db_session.refresh(ex2)
    assert stats2["retried"] == 1
    step2 = _step_by_key(db_session, ex2.id, "send")
    assert step2.error_code == "EXEC_REMINDER_DELIVERY_FAILED"


def test_reminder_send_clock_injected_no_real_sleep(db_session, monkeypatch):
    """delivered_at 完全来自注入时钟；handler 模块不依赖真实当前时间。
    （无真实 sleep 已由源码扫描断言：reminder_send.py 无 sleep 调用。）"""
    clock = _inject_reminder(db_session, monkeypatch)
    task = _make_task(db_session)
    reminder = _make_reminder(db_session, task)
    execution = _make(
        db_session, payload={"reminder_id": reminder.id, "task_id": task.id, "message": "m"}
    )
    enqueue(db_session, execution.id)
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1", clock=clock)
    worker.run_once()
    db_session.refresh(reminder)
    assert _as_utc(reminder.delivered_at) == FIXED_NOW
    # 时钟推进后再次投递另一 reminder → 时间戳随之推进（同一注入时钟）
    clock.advance(120)
    task2 = _make_task(db_session, title="Later")
    rem2 = _make_reminder(db_session, task2)
    ex2 = _make(
        db_session,
        payload={"reminder_id": rem2.id, "task_id": task2.id, "message": "m"},
    )
    enqueue(db_session, ex2.id)
    ExecutionWorker(_session_factory(db_session), worker_id="w1", clock=clock).run_once()
    db_session.refresh(rem2)
    assert _as_utc(rem2.delivered_at) == FIXED_NOW + timedelta(minutes=2)


# =====================================================================
# C. documents.reindex：确认门 / 恰好一次 / 恢复与租约安全
# =====================================================================


def test_reindex_never_without_confirmation(db_session, monkeypatch):
    """无 GRANTED 确认绝不执行：EXEC_CONFIRMATION_REQUIRED（可重试等待），
    reindex 服务零调用，文档索引状态不变。"""
    _inject_reindex(db_session, monkeypatch)
    spy = _spy_reindex(monkeypatch)
    doc = _make_ready_document(db_session)
    before = doc.index_status
    execution = _make(db_session, execution_type="documents.reindex",
                      payload={"document_id": doc.id})
    enqueue(db_session, execution.id)
    stats = ExecutionWorker(_session_factory(db_session), worker_id="w1").run_once()
    db_session.refresh(execution)
    assert stats["retried"] == 1  # 等待确认：可重试（受 max_attempts 约束）
    assert execution.status == ExecutionStatus.QUEUED.value
    step = _step_by_key(db_session, execution.id, "reindex")
    assert step.status == StepStatus.FAILED.value
    assert step.error_code == "EXEC_CONFIRMATION_REQUIRED"
    assert step.requires_confirmation is True
    assert step.confirmation_status == ConfirmationStatus.PENDING.value
    types = _event_types(db_session, execution.id)
    assert "CONFIRMATION_REQUIRED" in types
    assert "RETRY_SCHEDULED" in types
    assert spy["n"] == 0  # 未确认绝不调用
    db_session.refresh(doc)
    assert doc.index_status == before  # 索引状态未被触碰


def test_reindex_granted_exactly_once(db_session, monkeypatch):
    """GRANTED → 恰好一次真实重建；终态后再次运行无二次调用。"""
    _inject_reindex(db_session, monkeypatch)
    spy = _spy_reindex(monkeypatch)
    doc = _make_ready_document(db_session)
    execution = _make(db_session, execution_type="documents.reindex",
                      payload={"document_id": doc.id})
    enqueue(db_session, execution.id)
    grant_confirmation(
        db_session, execution.id, "reindex",
        actor_type="user", actor_id="admin", clock=_clock(),
    )
    assert "CONFIRMATION_GRANTED" in _event_types(db_session, execution.id)
    stats = ExecutionWorker(_session_factory(db_session), worker_id="w1").run_once()
    db_session.refresh(execution)
    assert stats["succeeded"] == 1
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    step = _step_by_key(db_session, execution.id, "reindex")
    assert step.status == StepStatus.SUCCEEDED.value
    assert step.confirmation_status == ConfirmationStatus.GRANTED.value
    assert spy["n"] == 1  # 恰好一次
    # 终态 execution 再跑 → 无 claim、零二次调用
    ExecutionWorker(_session_factory(db_session), worker_id="w1").run_once()
    assert spy["n"] == 1
    # 真实结果落库：INDEXED + 向量/关键词行写入（local-hash 真实离线路径）
    db_session.refresh(doc)
    assert doc.index_status == DocumentIndexStatus.INDEXED.value
    assert doc.index_error_message is None
    assert "embeddings_created" in step.output_json


def test_reindex_rejected_never(db_session, monkeypatch):
    """REJECTED → 稳定失败（不重试），reindex 服务零调用。"""
    _inject_reindex(db_session, monkeypatch)
    spy = _spy_reindex(monkeypatch)
    doc = _make_ready_document(db_session)
    before = doc.index_status
    execution = _make(db_session, execution_type="documents.reindex",
                      payload={"document_id": doc.id})
    enqueue(db_session, execution.id)
    reject_confirmation(
        db_session, execution.id, "reindex",
        actor_type="user", actor_id="bob", clock=_clock(),
    )
    assert "CONFIRMATION_REJECTED" in _event_types(db_session, execution.id)
    stats = ExecutionWorker(_session_factory(db_session), worker_id="w1").run_once()
    db_session.refresh(execution)
    assert stats["failed"] == 1  # 稳定失败，不重试
    assert execution.status == ExecutionStatus.FAILED.value
    assert execution.last_error_code == "EXEC_CONFIRMATION_REJECTED"
    assert "RETRY_SCHEDULED" not in _event_types(db_session, execution.id)
    step = _step_by_key(db_session, execution.id, "reindex")
    assert step.confirmation_status == ConfirmationStatus.REJECTED.value
    assert step.confirmation_actor_id == "bob"
    assert spy["n"] == 0
    db_session.refresh(doc)
    assert doc.index_status == before


def test_reindex_expired_on_payload_drift(db_session, monkeypatch):
    """确认绑定输入 hash：payload 漂移 → 旧 GRANTED 立即 EXPIRED，
    绝不跨 hash 执行（错误码 EXEC_CONFIRMATION_EXPIRED，可重试等待重确认）。"""
    clock = _inject_reindex(db_session, monkeypatch)
    spy = _spy_reindex(monkeypatch)
    doc_a = _make_ready_document(db_session, name="a.txt")
    doc_b = _make_ready_document(db_session, name="b.txt")
    execution = _make(db_session, execution_type="documents.reindex",
                      payload={"document_id": doc_a.id})
    enqueue(db_session, execution.id, clock=clock)
    grant_confirmation(
        db_session, execution.id, "reindex",
        actor_type="user", actor_id="admin", clock=clock,
    )
    # 模拟执行前的 payload 漂移（规范化格式 + hash 同步更新）
    drift = json.dumps(
        {"document_id": doc_b.id}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )
    execution.payload = drift
    execution.normalized_payload_hash = hashlib.sha256(
        drift.encode("utf-8")
    ).hexdigest()
    db_session.commit()
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1", clock=clock)
    stats = worker.run_once()
    db_session.refresh(execution)
    assert stats["retried"] == 1  # 可重试：等待重新确认
    assert execution.status == ExecutionStatus.QUEUED.value
    assert spy["n"] == 0  # 过期后绝不执行
    step = _step_by_key(db_session, execution.id, "reindex")
    assert step.status == StepStatus.FAILED.value
    assert step.error_code == "EXEC_CONFIRMATION_EXPIRED"
    assert step.confirmation_status == ConfirmationStatus.EXPIRED.value
    assert step.input_hash == _hash_of({"document_id": doc_b.id})  # 指纹已同步
    # 重新确认（绑定新 hash）→ 放行且恰好一次
    clock.advance(3600)  # 越过首次失败的 retry_after（backoff）
    grant_confirmation(
        db_session, execution.id, "reindex",
        actor_type="user", actor_id="admin", clock=clock,
    )
    stats2 = worker.run_once()
    db_session.refresh(execution)
    assert stats2["succeeded"] == 1
    assert execution.status == ExecutionStatus.SUCCEEDED.value
    assert spy["n"] == 1
    db_session.refresh(doc_b)
    assert doc_b.index_status == DocumentIndexStatus.INDEXED.value
    db_session.refresh(doc_a)
    assert doc_a.index_status != DocumentIndexStatus.INDEXED.value  # a 未被索引


def test_reindex_confirmation_ownership_isolated(db_session, monkeypatch):
    """确认绑定 (execution_id, step)：对 A 的确认绝不放行 B。"""
    _inject_reindex(db_session, monkeypatch)
    spy = _spy_reindex(monkeypatch)
    doc_a = _make_ready_document(db_session, name="a.txt")
    doc_b = _make_ready_document(db_session, name="b.txt")
    ex_a = _make(db_session, execution_type="documents.reindex",
                 payload={"document_id": doc_a.id})
    ex_b = _make(db_session, execution_type="documents.reindex",
                 payload={"document_id": doc_b.id})
    enqueue(db_session, ex_a.id)
    enqueue(db_session, ex_b.id)
    grant_confirmation(
        db_session, ex_a.id, "reindex",
        actor_type="user", actor_id="admin", clock=_clock(),
    )
    worker = ExecutionWorker(_session_factory(db_session), worker_id="w1")
    stats = worker.run_once()  # 只 claim 一个（ex_a）
    assert stats["succeeded"] == 1
    assert spy["n"] == 1
    stats2 = worker.run_once()  # ex_b：无自己的 GRANTED → 确认门拒绝
    db_session.refresh(ex_b)
    assert stats2["retried"] == 1
    assert ex_b.status == ExecutionStatus.QUEUED.value
    assert ex_b.last_error_code == "EXEC_CONFIRMATION_REQUIRED"
    assert spy["n"] == 1  # B 未执行


def test_reindex_replay_unsafe_blocks_rerun(db_session, monkeypatch):
    """崩溃恢复规则：RUNNING 的 destructive 步骤绝不重放
    （EXEC_STEP_REPLAY_UNSAFE，稳定失败）。"""
    _inject_reindex(db_session, monkeypatch)
    spy = _spy_reindex(monkeypatch)
    doc = _make_ready_document(db_session)
    execution = _make(db_session, execution_type="documents.reindex",
                      payload={"document_id": doc.id})
    enqueue(db_session, execution.id)
    clock = FakeClock()
    claimed = claim_next(db_session, worker_id="crashy", clock=clock, lease_ttl_seconds=60)
    assert claimed is not None
    execution, attempt = claimed
    transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
    db_session.add(
        ExecutionStep(
            execution_id=execution.id, attempt_id=attempt.id,
            step_key="reindex", step_index=0, tool_name="documents.reindex",
            status=StepStatus.RUNNING.value,
            input_json=json.dumps({"document_id": doc.id}, sort_keys=True),
            input_hash=_hash_of({"document_id": doc.id}),
            requires_confirmation=True,
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
    assert execution.last_error_code == "EXEC_STEP_REPLAY_UNSAFE"
    assert spy["n"] == 0  # 绝不重放
    step = _step_by_key(db_session, execution.id, "reindex")
    assert step.status == StepStatus.FAILED.value
    assert step.error_code == "EXEC_STEP_REPLAY_UNSAFE"
    assert "RETRY_SCHEDULED" not in _event_types(db_session, execution.id)


def test_reindex_stale_worker_blocks_at_first_write(db_session, monkeypatch):
    """租约被回收后 stale worker 的编排在第一次写入即 EXEC_CLAIM_LOST，
    不写任何 step/event/terminal 状态。"""
    _inject_reindex(db_session, monkeypatch)
    spy = _spy_reindex(monkeypatch)
    doc = _make_ready_document(db_session)
    execution = _make(db_session, execution_type="documents.reindex",
                      payload={"document_id": doc.id})
    enqueue(db_session, execution.id)
    clock = FakeClock()
    claimed = claim_next(db_session, worker_id="stale-w", clock=clock, lease_ttl_seconds=60)
    assert claimed is not None
    execution, attempt = claimed
    transition_to(db_session, execution.id, ExecutionStatus.RUNNING, clock=clock)
    clock.advance(61)
    recover_expired(db_session, clock=clock)  # 租约被回收
    events_after_recover = len(list_events(db_session, execution.id))
    with pytest.raises(ExecutionClaimLostError):
        run_execution_steps(db_session, execution, attempt, clock=clock)
    assert (
        db_session.query(ExecutionStep)
        .filter(ExecutionStep.execution_id == execution.id)
        .count()
        == 0
    )
    assert len(list_events(db_session, execution.id)) == events_after_recover
    assert spy["n"] == 0
    db_session.refresh(doc)
    assert doc.index_status != DocumentIndexStatus.INDEXED.value


def test_reindex_real_service_invoked_not_test_double(db_session, monkeypatch):
    """spy（包装真实实现，保留描述符绑定）证明 handler 调用的是真实
    RetrievalIndexService.reindex_document 业务路径，且真实副作用落库。"""
    _inject_reindex(db_session, monkeypatch)
    spy = _spy_reindex(monkeypatch)
    doc = _make_ready_document(db_session)
    execution = _make(db_session, execution_type="documents.reindex",
                      payload={"document_id": doc.id})
    enqueue(db_session, execution.id)
    grant_confirmation(
        db_session, execution.id, "reindex",
        actor_type="user", actor_id="admin", clock=_clock(),
    )
    stats = ExecutionWorker(_session_factory(db_session), worker_id="w1").run_once()
    assert stats["succeeded"] == 1
    assert spy["n"] == 1
    assert spy["args"][0] == doc.id  # 真实参数：handler 传入的 document_id
    # 真实结果：向量行存在（local-hash 384 维真实写入）
    db_session.refresh(doc)
    assert doc.index_status == DocumentIndexStatus.INDEXED.value
    chunk_rows = (
        db_session.query(ChunkEmbedding)
        .filter(
            ChunkEmbedding.chunk_id.in_(
                db_session.query(DocumentChunk.id).filter(
                    DocumentChunk.document_id == doc.id
                )
            )
        )
        .count()
    )
    assert chunk_rows >= 1
