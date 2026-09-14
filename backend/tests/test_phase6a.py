"""Phase 6A 定向测试：Execution Domain Foundation。

覆盖：模型注册与持久化、幂等（重放/冲突/并发唯一约束裁决）、状态机全矩阵
（合法/非法/终态不可逆）、类型注册表与 payload fail-closed、脱敏、迁移
往返（SQLite）+ PostgreSQL 离线 SQL 生成。

全部使用临时 SQLite 库（tmp_path），绝不触碰 jarvis.db；时钟用 FixedClock，
不依赖真实当前时间；无网络、无 Docker。
"""

import itertools
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from app.core.clock import FixedClock
from app.core.config import settings
from app.core.database import Base, _set_sqlite_pragma
from app.core.sanitize import (
    DENY_FIELD_NAMES,
    is_denied_field_name,
    sanitize_error_message,
)
from app.models.execution import TERMINAL_STATUSES, Execution, ExecutionStatus
from app.services.execution_errors import (
    ExecutionError,
    ExecutionNotFoundError,
    ExecutionPayloadError,
    ExecutionTypeUnknownError,
    IdempotencyConflictError,
    InvalidExecutionTransitionError,
    InvalidIdempotencyKeyError,
)
from app.services.execution_registry import EXECUTION_TYPES, ExecutionTypeSpec
from app.services.execution_service import (
    ALLOWED_EXECUTION_TRANSITIONS,
    create_execution,
    enqueue,
    get_execution,
    request_cancel,
    transition_to,
)
from tests.conftest import FIXED_NOW

UTC = timezone.utc
KEY = "test-key-0001"
PING = {}  # system.ping 白名单为空：payload 只能为空对象

BACKEND_DIR = Path(__file__).resolve().parents[1]

EXPECTED_COLUMNS = {
    "id",
    "owner_id",
    "execution_type",
    "status",
    "payload",
    "normalized_payload_hash",
    "idempotency_key",
    "run_at",
    "correlation_id",
    "queued_at",
    "started_at",
    "finished_at",
    "cancel_requested_at",
    "attempt_count",
    "max_attempts",
    "timeout_seconds",
    "retry_after",
    "lease_owner",
    "lease_expires_at",
    "last_error_code",
    "last_error_message",
    "retry_of_id",
    "created_at",
    "updated_at",
}

# 全部 9 态
ALL_STATUSES = list(ExecutionStatus)


@pytest.fixture()
def db_session(tmp_path):
    """独立临时 SQLite 库 + session（不依赖 TestClient，纯领域层测试）。"""
    db_path = tmp_path / "test_executions.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


def _clock():
    return FixedClock(FIXED_NOW)


def _now_naive():
    return FIXED_NOW.replace(tzinfo=None)


# 每个用例独立幂等键：同一测试内多次创建绝不被幂等重放吞并
_key_counter = itertools.count(1)


def _unique_key() -> str:
    return f"test-key-{next(_key_counter):08d}"


def _make(db, key=None, payload=PING, **kwargs):
    return create_execution(
        db,
        execution_type="system.ping",
        payload=payload,
        idempotency_key=key or _unique_key(),
        clock=_clock(),
        **kwargs,
    )


def _walk(db, execution: Execution, *statuses: ExecutionStatus) -> Execution:
    """在同一 execution 上连续执行合法转换（矩阵测试辅助）。"""
    for target in statuses:
        transition_to(db, execution.id, target, clock=_clock())
    return get_execution(db, execution.id)


def _reach(db, target: ExecutionStatus) -> Execution:
    """把新 execution 沿合法路径走到 target（矩阵测试辅助）。

    路径（唯一确定性选择，避免同状态多路径导致语义歧义）：
    - CREATED: 直接
    - QUEUED: enqueue
    - CLAIMED: QUEUED → CLAIMED
    - RUNNING: QUEUED → CLAIMED → RUNNING
    - CANCEL_REQUESTED: QUEUED → CANCEL_REQUESTED（两跳路径）
    - SUCCEEDED / FAILED / TIMED_OUT: QUEUED → CLAIMED → RUNNING → 终态
    - CANCELLED: QUEUED → CANCELLED
    """
    execution = _make(db)
    if target is ExecutionStatus.CREATED:
        return execution
    _walk(db, execution, ExecutionStatus.QUEUED)
    if target is ExecutionStatus.QUEUED:
        return execution
    if target is ExecutionStatus.CANCELLED:
        return _walk(db, execution, ExecutionStatus.CANCELLED)
    if target is ExecutionStatus.CANCEL_REQUESTED:
        return _walk(db, execution, ExecutionStatus.CANCEL_REQUESTED)
    _walk(db, execution, ExecutionStatus.CLAIMED)
    if target is ExecutionStatus.CLAIMED:
        return execution
    _walk(db, execution, ExecutionStatus.RUNNING)
    if target is ExecutionStatus.RUNNING:
        return execution
    return _walk(db, execution, target)


def _make_config(db_file: Path) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


# ---- 模型注册与持久化 ----


def test_models_init_registers_execution_table():
    assert "executions" in Base.metadata.tables


def test_create_execution_persists_all_fields(db_session):
    run_at = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    execution = _make(
        db_session,
        key=KEY,
        owner_id="alice",
        run_at=run_at,
        correlation_id="corr-1234567890abcdef",
    )
    assert execution.id is not None
    assert execution.owner_id == "alice"
    assert execution.execution_type == "system.ping"
    assert execution.status == ExecutionStatus.CREATED.value
    assert execution.payload == "{}"
    assert len(execution.normalized_payload_hash) == 64
    assert execution.idempotency_key == KEY
    assert execution.correlation_id == "corr-1234567890abcdef"
    assert execution.max_attempts == 3
    assert execution.timeout_seconds is None
    # 时间：FixedClock 精确断言（SQLite naive UTC）
    assert execution.created_at == _now_naive()
    assert execution.queued_at is None
    assert execution.started_at is None
    assert execution.finished_at is None
    # run_at 只存储，不调度：状态仍 CREATED（无自动入队/执行）
    assert execution.run_at == run_at.replace(tzinfo=None)
    assert execution.status == ExecutionStatus.CREATED.value


def test_default_owner_and_key_fields(db_session):
    assert _make(db_session).owner_id == "default"


# ---- 幂等 ----


def test_replay_same_key_same_request_returns_existing(db_session):
    first = _make(db_session, key=KEY)
    # 中间推进状态：重放应返回同一资源（含当前状态）
    _walk(db_session, first, ExecutionStatus.QUEUED)
    replay = _make(db_session, key=KEY)
    assert replay.id == first.id
    assert replay.status == ExecutionStatus.QUEUED.value


def test_replay_order_insensitive_payload(db_session, monkeypatch):
    """键顺序不同的等价 payload → 同一规范化哈希 → 重放。

    system.ping 白名单为空，无法用多字段 payload；注册一个测试专用多字段
    type 验证规范化（registry 本身是配置级 dict，monkeypatch 恢复）。
    """
    spec = ExecutionTypeSpec(
        name="test.multi",
        payload_schema_version=1,
        payload_allowed_fields=frozenset({"a", "b"}),
        max_attempts=3,
        timeout_seconds=None,
    )
    monkeypatch.setitem(EXECUTION_TYPES, "test.multi", spec)
    first = create_execution(
        db_session,
        execution_type="test.multi",
        payload={"b": 2, "a": 1},
        idempotency_key=KEY,
        clock=_clock(),
    )
    replay = create_execution(
        db_session,
        execution_type="test.multi",
        payload={"a": 1, "b": 2},
        idempotency_key=KEY,
        clock=_clock(),
    )
    assert replay.id == first.id
    assert first.normalized_payload_hash == replay.normalized_payload_hash


def test_conflict_same_key_different_request(db_session):
    """同 owner+type+key、规范化请求不同 → 稳定 409 冲突。

    system.ping 白名单只允许 {}，payload 无法真实变化；篡改已有行的请求
    指纹模拟"同 key 但请求不同"（未来多字段 type 注册后由真实 payload
    差异走同一路径），验证裁决逻辑与错误码。
    """
    execution = _make(db_session, key=KEY)
    execution.normalized_payload_hash = "f" * 64
    db_session.commit()
    with pytest.raises(IdempotencyConflictError) as exc:
        _make(db_session, key=KEY)
    assert exc.value.code == "EXEC_IDEMPOTENCY_CONFLICT"
    assert str(execution.id) in str(exc.value)


def test_different_owner_scope_does_not_conflict(db_session):
    """幂等作用域含 owner：同 key 不同 owner 是不同资源（不冲突）。"""
    first = _make(db_session, key=KEY)
    second = _make(db_session, key=KEY, owner_id="another-owner")
    assert second.id != first.id
    assert second.owner_id == "another-owner"


def test_idempotency_key_validation(db_session):
    for bad in ("short", "x" * 129, "bad key!", "has/slash"):
        with pytest.raises(InvalidIdempotencyKeyError) as exc:
            _make(db_session, key=bad)
        assert exc.value.code == "EXEC_IDEMPOTENCY_KEY_INVALID"


def test_concurrent_duplicate_key_resolved_by_unique_constraint(tmp_path):
    """两个并发请求同 key：唯一约束裁决，数据库恰一行，双方稳定返回。

    两个独立线程各自通过"先查后写"快速路径（都未命中），随后 commit——
    由 UNIQUE 约束 + IntegrityError 捕获裁决，绝不依赖先查后写。
    """
    db_path = tmp_path / "concurrent.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    results: list[tuple[str, int | str]] = []
    barrier = threading.Barrier(2)

    def worker():
        session = SessionLocal()
        try:
            barrier.wait()
            execution = create_execution(
                session,
                execution_type="system.ping",
                payload={},
                idempotency_key=KEY,
                clock=FixedClock(FIXED_NOW),
            )
            results.append(("ok", execution.id))
        except ExecutionError as exc:
            results.append(("error", exc.code))
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 2
    ids = {result[1] for result in results if result[0] == "ok"}
    assert len(ids) == 1, f"两个并发请求必须解析为同一 execution: {results}"

    session = SessionLocal()
    try:
        assert session.query(Execution).count() == 1
    finally:
        session.close()


# ---- 类型注册表与 payload fail-closed ----


def test_unknown_type_rejected(db_session):
    with pytest.raises(ExecutionTypeUnknownError) as exc:
        create_execution(
            db_session,
            execution_type="nope.unknown",
            payload={},
            idempotency_key=KEY,
            clock=_clock(),
        )
    assert exc.value.code == "EXEC_TYPE_UNKNOWN"


def test_payload_must_be_object(db_session):
    with pytest.raises(ExecutionPayloadError) as exc:
        _make(db_session, payload=["not", "an", "object"])
    assert exc.value.code == "EXEC_PAYLOAD_INVALID"


def test_payload_disallowed_field_rejected(db_session):
    """system.ping 白名单为空：任何字段都拒绝（fail-closed，绝不静默丢弃）。"""
    with pytest.raises(ExecutionPayloadError) as exc:
        _make(db_session, payload={"message": "hello"})
    assert exc.value.code == "EXEC_PAYLOAD_INVALID"


def test_payload_sensitive_field_name_rejected(db_session):
    """敏感字段名即使在白名单内也拒绝（第二层防御）。"""
    with pytest.raises(ExecutionPayloadError) as exc:
        _make(db_session, payload={"api_key": "sk-123"})
    assert exc.value.code == "EXEC_PAYLOAD_INVALID"


# ---- 状态机 ----


def test_allowed_transitions_matrix(db_session):
    """白名单内每一对转换都可执行且状态正确。"""
    for current in ALL_STATUSES:
        for target in ALLOWED_EXECUTION_TRANSITIONS[current]:
            execution = _reach(db_session, current)
            transition_to(db_session, execution.id, target, clock=_clock())
            assert get_execution(db_session, execution.id).status == target.value


def test_invalid_transitions_all_rejected(db_session):
    """全部非白名单转换 → EXEC_INVALID_STATE_TRANSITION，状态不变。"""
    for current in ALL_STATUSES:
        allowed = ALLOWED_EXECUTION_TRANSITIONS[current]
        for target in ALL_STATUSES:
            if target in allowed:
                continue
            execution = _reach(db_session, current)
            before = get_execution(db_session, execution.id).status
            with pytest.raises(InvalidExecutionTransitionError) as exc:
                transition_to(db_session, execution.id, target, clock=_clock())
            assert exc.value.code == "EXEC_INVALID_STATE_TRANSITION"
            assert get_execution(db_session, execution.id).status == before


def test_terminal_statuses_are_frozen(db_session):
    """终态无出边：任何转换（含 TIMED_OUT 标记与重入自身）都被拒绝。

    唯一例外是 6E Retry API 规格八授权的人工重试入口 FAILED → QUEUED
    （CAS 条件 UPDATE，见 execution_service.retry_execution）。
    """
    for terminal in TERMINAL_STATUSES:
        execution = _reach(db_session, terminal)
        assert execution.status == terminal.value
        assert execution.finished_at == _now_naive()
        for target in ALL_STATUSES:
            if terminal is ExecutionStatus.FAILED and target is ExecutionStatus.QUEUED:
                continue  # 6E 授权例外：人工重试
            with pytest.raises(InvalidExecutionTransitionError):
                transition_to(db_session, execution.id, target, clock=_clock())
            assert get_execution(db_session, execution.id).status == terminal.value


def test_timestamps_set_on_transitions(db_session):
    execution = _make(db_session)
    # QUEUED → queued_at
    enqueue(db_session, execution.id, clock=_clock())
    execution = get_execution(db_session, execution.id)
    assert execution.queued_at == _now_naive()
    assert execution.started_at is None
    # RUNNING → started_at
    _walk(db_session, execution, ExecutionStatus.CLAIMED, ExecutionStatus.RUNNING)
    execution = get_execution(db_session, execution.id)
    assert execution.started_at == _now_naive()
    # 终态 → finished_at（不覆盖已存在的 started_at）
    transition_to(db_session, execution.id, ExecutionStatus.SUCCEEDED, clock=_clock())
    execution = get_execution(db_session, execution.id)
    assert execution.finished_at == _now_naive()
    assert execution.started_at == _now_naive()


def test_cancel_requested_timestamp(db_session):
    execution = _reach(db_session, ExecutionStatus.CANCEL_REQUESTED)
    assert execution.cancel_requested_at == _now_naive()


def test_enqueue_duplicate_rejected(db_session):
    execution = _make(db_session)
    enqueue(db_session, execution.id, clock=_clock())
    with pytest.raises(InvalidExecutionTransitionError) as exc:
        enqueue(db_session, execution.id, clock=_clock())
    assert exc.value.code == "EXEC_INVALID_STATE_TRANSITION"


def test_request_cancel_paths(db_session):
    # CREATED → 直接 CANCELLED（未入队）
    created = _make(db_session)
    cancelled = request_cancel(db_session, created.id, clock=_clock())
    assert cancelled.status == ExecutionStatus.CANCELLED.value
    assert cancelled.finished_at == _now_naive()

    # QUEUED → 直接 CANCELLED（未领取）
    queued = _reach(db_session, ExecutionStatus.QUEUED)
    assert request_cancel(db_session, queued.id, clock=_clock()).status == (
        ExecutionStatus.CANCELLED.value
    )

    # RUNNING → CANCEL_REQUESTED（登记，需 worker 协作——6B 执行动作）
    running = _reach(db_session, ExecutionStatus.RUNNING)
    pending = request_cancel(db_session, running.id, clock=_clock())
    assert pending.status == ExecutionStatus.CANCEL_REQUESTED.value
    assert pending.cancel_requested_at == _now_naive()

    # CANCEL_REQUESTED 重复取消 → 幂等
    again = request_cancel(db_session, running.id, clock=_clock())
    assert again.id == pending.id
    assert again.status == ExecutionStatus.CANCEL_REQUESTED.value

    # 终态取消 → 拒绝
    done = _reach(db_session, ExecutionStatus.SUCCEEDED)
    with pytest.raises(InvalidExecutionTransitionError):
        request_cancel(db_session, done.id, clock=_clock())


def test_get_execution_not_found(db_session):
    with pytest.raises(ExecutionNotFoundError) as exc:
        get_execution(db_session, 99999)
    assert exc.value.code == "EXEC_NOT_FOUND"


def test_run_at_stored_not_scheduled(db_session):
    """run_at 只存储：状态保持 CREATED，不自动入队（调度属 6D）。"""
    future = FIXED_NOW + timedelta(hours=2)
    execution = _make(db_session, run_at=future)
    assert execution.run_at == future.replace(tzinfo=None)
    assert execution.status == ExecutionStatus.CREATED.value


# ---- 脱敏 ----


def test_sanitize_removes_database_url_and_secrets():
    message = f"connect failed to {settings.database_url} with key sk-secret-abc123"
    out = sanitize_error_message(message)
    assert "<redacted>" in out
    assert settings.database_url not in out


def test_sanitize_truncates_long_messages():
    long_message = "x" * 2000
    out = sanitize_error_message(long_message)
    assert len(out) == 500
    assert out.endswith("x" * 400)  # 保留前缀，截断尾部


def test_deny_field_names():
    for name in ("api_key", "password", "token", "secret", "my_token", "endpoint_token"):
        assert is_denied_field_name(name), name
    for name in ("title", "content", "message", "count"):
        assert not is_denied_field_name(name), name
    assert "api_key" in DENY_FIELD_NAMES


# ---- 迁移 ----


def test_migration_sqlite_roundtrip(tmp_path):
    """upgrade head → 表结构与约束正确且可承载数据 → downgrade（表移除、
    既有表保留）→ re-upgrade（恢复）。"""
    db_file = tmp_path / "migrate6a.db"
    cfg = _make_config(db_file)
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_file}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "executions" in tables
    assert "tasks" in tables  # 既有封存表不受影响

    columns = {col["name"] for col in inspector.get_columns("executions")}
    assert columns == EXPECTED_COLUMNS

    # SQLite inspector 语义：op.create_index(unique=True) 生成的唯一索引
    # 出现在 get_indexes（带 unique 标志），而非 get_unique_constraints
    # （后者只报表级 UNIQUE 约束）
    indexes = {idx["name"]: idx["unique"] for idx in inspector.get_indexes("executions")}
    assert indexes.get("uq_executions_idempotency")  # SQLite 返回 1/0 整数
    assert {"ix_executions_id", "ix_executions_status", "ix_executions_status_run_at"} <= set(indexes)

    fks = inspector.get_foreign_keys("executions")
    assert any(
        fk["referred_table"] == "executions"
        and fk.get("options", {}).get("ondelete") == "SET NULL"
        for fk in fks
    )

    # 迁移后的库可承载真实数据（幂等唯一约束真实生效）
    engine2 = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    event.listen(engine2, "connect", _set_sqlite_pragma)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine2)
    session = SessionLocal()
    session.add(
        Execution(
            owner_id="default",
            execution_type="system.ping",
            status=ExecutionStatus.CREATED.value,
            payload="{}",
            normalized_payload_hash="a" * 64,
            idempotency_key="migrated-key-0001",
            created_at=FIXED_NOW.replace(tzinfo=None),
        )
    )
    session.commit()
    session.close()

    # downgrade 到封存 head：executions 移除，既有表保留
    command.downgrade(cfg, "e6f5d4c3b2a1")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "executions" not in tables
    assert "tasks" in tables
    assert "alembic_version" in tables

    # re-upgrade：结构恢复
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert "executions" in set(inspector.get_table_names())
    assert columns == EXPECTED_COLUMNS


def test_pg_offline_sql_generation(capsys):
    """PostgreSQL 离线 SQL 生成：不连接真实 PG，检查编译出的 DDL。"""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://user:pass@localhost:5432/jarvis_pg_check",
    )
    command.upgrade(cfg, "head", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE TABLE executions" in sql
    assert "CREATE UNIQUE INDEX uq_executions_idempotency" in sql
    assert "ix_executions_status_run_at" in sql
    assert "ON DELETE SET NULL" in sql
    # 注：offline SQL 是完整迁移链（含 5B pgvector），其 CREATE EXTENSION
    # 属既有已封存迁移的正常输出，不在本测试范围。
