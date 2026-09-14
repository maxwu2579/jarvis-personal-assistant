"""Phase 6F 真实 PostgreSQL Gate（显式门控，默认 SKIPPED）。

Phase 6F 授权边界：真实 PG 上的集成验证，不新增产品功能。
本模块证明 Phase 6A–6E 在真实 PostgreSQL（16 + pgvector 镜像）下成立。

门控（与 test_postgres_integration 同模式）：
- RUN_POSTGRES_INTEGRATION=1 未设置 → 整模块 SKIPPED（绝不连接、不伪造）；
- 已启用但 TEST_POSTGRES_URL 缺失 → SKIP；
- 数据库名未以 jarvis_phase6f 开头 → FAIL（Phase 6F 独立命名强制，
  比 5A 黑名单更严：任何非 phase6f 专用库绝不连接）；
- 命中生产库名单（postgres/template0/template1/jarvis）→ FAIL。

验证内容（Phase 6F Gate 2 六项）：
  1. Alembic 全链：upgrade head → 单 head → downgrade 上一阶段 → re-upgrade
     → 表/索引/FK/UNIQUE/CAS 依赖结构；
  2. 多 Worker 原子 claim：独立连接并发，至多一个成功，attempt 不重复，
     loser 无 attempt/event/终态副作用；
  3. Idempotency-Key 并发：同 key 同请求单一创建其余 replay；
     同 key 异请求稳定 409；重启/新进程后由数据库约束保证；
  4. Lease 与 stale worker：中断 → 过期回收 → renew/finish/step 全拒，
     无幽灵终态、无重复执行记录；
  5. 竞争裁决：cancel vs success / timeout vs success / misfire SKIP/FAIL
     vs claim / confirmation 同 action 幂等、不同 action 不可翻转；
  6. steps/events：唯一约束、sequence 单调、回滚同消、敏感字段脱敏、
     destructive 未确认绝不执行、REJECTED 后绝不执行。

禁止事项（Phase 6F）：不用 sleep 制造竞态（并发用 Barrier 同步起跑、
时钟用 FakeClock 推进）、不用 SQLite 代替、不伪造真实 PG 结果。
"""

import itertools
import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core.clock import FixedClock
from app.core.database import Base
from app.models.execution import Execution, ExecutionStatus
from app.models.execution_attempt import AttemptStatus, ExecutionAttempt
from app.models.execution_event import ExecutionEvent
from app.models.execution_step import ExecutionStep
from app.services.execution_claim import (
    check_timeouts,
    claim_next,
    recover_expired,
    renew_lease,
)
from app.services.execution_errors import (
    ExecutionClaimLostError,
    IdempotencyConflictError,
    StepConflictError,
)
from app.services.execution_events import append_event, list_events
from app.services.execution_registry import (
    EXECUTION_HANDLERS,
    EXECUTION_TOOLS,
    EXECUTION_TYPES,
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
from app.services.execution_steps import (
    grant_confirmation,
    reject_confirmation,
    run_execution_steps,
)
from app.services.execution_worker import ExecutionWorker

BACKEND_DIR = Path(__file__).resolve().parents[1]
HEAD_REVISION = "8c9d0e1f2a3b4"  # 6C（6D/6E 零迁移，head 未变）
PREVIOUS_REVISION = "7b8c9d0e1f2a3"  # 6B（head 的上一阶段）

RUN = os.environ.get("RUN_POSTGRES_INTEGRATION") == "1"
TEST_URL = os.environ.get("TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not RUN,
    reason="RUN_POSTGRES_INTEGRATION=1 未设置：Phase 6F 真实 PG Gate 默认跳过",
)

_FORBIDDEN_DB_NAMES = {"postgres", "template0", "template1", "jarvis"}
_PHASE6F_DB_PREFIX = "jarvis_phase6f"

FIXED_NOW = datetime(2026, 8, 21, 4, 0, tzinfo=timezone.utc)


class FakeClock:
    """可推进时钟（模拟 lease 过期 / timeout / misfire 时间流逝）。"""

    def __init__(self, start=FIXED_NOW):
        self._now = start

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)


_key_counter = itertools.count(1)


def _unique_key() -> str:
    return f"f6-pg-key-{next(_key_counter):08d}"


def _make_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _reset_schema(engine) -> None:
    """把库恢复到「无表」状态（terminate → 删 pg_chunk_vectors → drop_all）。

    - 先断开本测试库的全部遗留应用连接（失败测试泄漏、未 close 的会话
      持有 AccessShareLock，会让 drop_all 死锁在 ACCESS EXCLUSIVE 上，
      实测曾挂死整个 suite）。只作用于当前 jarvis_phase6f_* 测试库；
    - pg_chunk_vectors 不在 Base.metadata（PG 专属表，5B）且 FK 引用
      document_chunks，必须先删它 drop_all 才能继续（5A 同款模式）；
    - alembic_version 一并删除，让下一次 upgrade 从空库全链执行。
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = current_database() "
                "AND pid <> pg_backend_pid()"
            )
        )
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS pg_chunk_vectors CASCADE"))
    Base.metadata.drop_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))


@pytest.fixture
def pg_env():
    """门控 → 连接 phase6f 专用库 → 干净 schema → upgrade head。

    函数级作用域：每个测试独占干净 schema，与 SQLite 测试「每测试一个
    tmp 库」语义一致。实测发现模块级共享库会互相污染——前测遗留的
    QUEUED/CLAIMED 行会被后续测试的 claim_next/recover_expired 捡走
    （claim 无条件按到期候选取行，不按 execution_type 过滤）。
    """
    if not TEST_URL:
        pytest.skip(
            "RUN_POSTGRES_INTEGRATION=1 但 TEST_POSTGRES_URL 未设置 —— "
            "需要 jarvis_phase6f_* 专用测试库"
        )
    from sqlalchemy.engine import make_url

    url = make_url(TEST_URL)
    db_name = url.database or ""
    if db_name in _FORBIDDEN_DB_NAMES:
        pytest.fail(f"拒绝在疑似生产库上运行 Phase 6F Gate: {db_name!r}")
    if not db_name.startswith(_PHASE6F_DB_PREFIX):
        pytest.fail(
            f"Phase 6F 强制 jarvis_phase6f_* 独立命名，当前库 {db_name!r} 拒绝连接"
        )
    print(
        f"[phase6f-pg] host={url.host} port={url.port} database={db_name} "
        f"driver={url.drivername}"
    )

    engine = create_engine(TEST_URL, pool_pre_ping=True)
    # 与生产 app/core/database.py 一致：为每个连接注册 pgvector 适配器
    # （5B 迁移含 vector 类型，迁移/查询路径需要）。
    from pgvector.psycopg import register_vector

    @event.listens_for(engine, "connect")
    def _register_pg_vector_adapter(dbapi_connection, connection_record):
        register_vector(dbapi_connection)

    # 干净起点：上一次运行残留（失败 teardown / 中断）不污染本次——
    # 幂等键计数器每进程重置，残留行会让 create_execution 重放旧 execution。
    _reset_schema(engine)
    command.upgrade(_make_config(TEST_URL), "head")
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        yield engine, SessionLocal
    finally:
        # 清理：只删本次测试创建的表（数据库/容器由外层脚本负责删除）
        try:
            _reset_schema(engine)
        finally:
            engine.dispose()


# ---- 领域层辅助（沿 6A-6E 既有模式；服务自提交，多会话可见） ----


def _make(db, *, execution_type="system.ping", payload=None, **kwargs):
    return create_execution(
        db,
        execution_type=execution_type,
        payload=payload if payload is not None else {},
        idempotency_key=_unique_key(),
        clock=FixedClock(FIXED_NOW),
        **kwargs,
    )


def _enqueue(db, execution):
    enqueue(db, execution.id, clock=FixedClock(FIXED_NOW))
    return execution


def _claim(db, worker_id="worker-a", clock=None):
    return claim_next(
        db, worker_id, clock=clock or FixedClock(FIXED_NOW), lease_ttl_seconds=60
    )


def _register_type(name, *, payload_fields=frozenset(), steps=(),
                   max_attempts=3, timeout_seconds=None,
                   misfire_policy=None, misfire_grace_seconds=None):
    """注册测试类型（try/finally 配对 _unregister_type，测试后清除）。

    steps=None → legacy 单 handler 模式（合成单步）；steps=() 恒为
    显式空模板（StepInvalidError，绝不含糊）。
    """
    kwargs = dict(
        name=name,
        payload_schema_version=1,
        payload_allowed_fields=frozenset(payload_fields),
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
        retry_backoff_base_seconds=0.001,
        retry_backoff_factor=2.0,
        steps=None if steps is None else tuple(steps),
    )
    if misfire_policy is not None:
        kwargs["misfire_policy"] = misfire_policy
    if misfire_grace_seconds is not None:
        kwargs["misfire_grace_seconds"] = misfire_grace_seconds
    EXECUTION_TYPES[name] = ExecutionTypeSpec(**kwargs)


def _register_tool(name, *, handler, destructive=False,
                   requires_confirmation=False, replay_safe=True,
                   retryable=True):
    EXECUTION_TOOLS[name] = ToolSpec(
        name=name,
        handler=handler,
        input_schema=None,
        output_schema=None,
        destructive=destructive,
        requires_confirmation=requires_confirmation,
        retryable=retryable,
        replay_safe=replay_safe,
        sanitize_result=True,
        enabled=True,
    )


def _unregister_type(name):
    EXECUTION_TYPES.pop(name, None)
    EXECUTION_HANDLERS.pop(name, None)


def _unregister_tool(name):
    EXECUTION_TOOLS.pop(name, None)


# =====================================================================
# 1. Alembic 全链：upgrade → 单 head → downgrade → re-upgrade → 结构
# =====================================================================


def test_alembic_full_chain_and_structure(pg_env):
    engine, SessionLocal = pg_env

    # 单 head
    script = ScriptDirectory(str(BACKEND_DIR / "alembic"))
    assert script.get_heads() == [HEAD_REVISION]

    # downgrade 到上一阶段（6B），6C 的步骤/事件表应消失
    command.downgrade(_make_config(TEST_URL), PREVIOUS_REVISION)
    insp = inspect(engine)
    assert "execution_steps" not in insp.get_table_names()
    assert "execution_events" not in insp.get_table_names()

    # re-upgrade head
    command.upgrade(_make_config(TEST_URL), "head")

    # 表齐备
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for t in ("executions", "execution_attempts", "execution_steps",
              "execution_events"):
        assert t in tables, f"缺失表 {t}"

    # UNIQUE 约束（CAS 依赖的数据库级裁决）
    def _unique_index(table, cols):
        return any(
            ix["unique"] and list(ix["column_names"]) == cols
            for ix in insp.get_indexes(table)
        )

    assert _unique_index("executions", ["owner_id", "execution_type", "idempotency_key"])
    assert _unique_index("execution_attempts", ["execution_id", "attempt_number"])
    assert _unique_index("execution_steps", ["execution_id", "step_index"])
    assert _unique_index("execution_steps", ["execution_id", "step_key"])
    assert _unique_index("execution_events", ["execution_id", "sequence_number"])

    # FK 与级联
    fk_exec = {
        (fk["constrained_columns"][0], fk["referred_table"])
        for fk in insp.get_foreign_keys("execution_attempts")
    }
    assert ("execution_id", "executions") in fk_exec
    for table in ("execution_steps", "execution_events"):
        fks = {
            (fk["constrained_columns"][0], fk["referred_table"])
            for fk in insp.get_foreign_keys(table)
        }
        assert ("execution_id", "executions") in fks, table

    # alembic_version 只有 head 一条
    with engine.connect() as conn:
        version = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    assert version == HEAD_REVISION


# =====================================================================
# 2. 多 Worker 原子 claim：真并发、单赢家、无副作用
# =====================================================================


def test_multi_worker_atomic_claim_single_winner(pg_env):
    engine, SessionLocal = pg_env
    db = SessionLocal()
    execution = _make(db)
    _enqueue(db, execution)
    execution_id = execution.id
    db.close()

    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    barrier = threading.Barrier(3)
    winners: list[str] = []
    errors: list[BaseException] = []

    def _try_claim(worker_id: str) -> None:
        s = factory()
        try:
            barrier.wait(timeout=15)
            claimed = claim_next(
                s, worker_id, clock=FixedClock(FIXED_NOW), lease_ttl_seconds=60
            )
            if claimed is not None:
                winners.append(worker_id)
        except BaseException as exc:  # noqa: BLE001 - 线程内捕获上报
            errors.append(exc)
        finally:
            s.close()

    threads = [
        threading.Thread(target=_try_claim, args=(f"w{i}",)) for i in range(2)
    ]
    for t in threads:
        t.start()
    barrier.wait(timeout=15)
    for t in threads:
        t.join(timeout=20)
    assert not errors, f"claim 线程异常: {errors}"
    assert len(winners) == 1, f"期望恰好一个赢家，实际 {winners}"

    # 只有一个 attempt，编号从 1 开始且不重复
    db = SessionLocal()
    attempts = (
        db.query(ExecutionAttempt)
        .filter(ExecutionAttempt.execution_id == execution_id)
        .order_by(ExecutionAttempt.attempt_number)
        .all()
    )
    assert len(attempts) == 1
    assert attempts[0].attempt_number == 1
    assert attempts[0].worker_id == winners[0]
    # loser 无副作用：无重复 claim 事件、无终态
    events = (
        db.query(ExecutionEvent)
        .filter(ExecutionEvent.execution_id == execution_id)
        .all()
    )
    claimed_events = [e for e in events if e.event_type == "EXECUTION_CLAIMED"]
    assert len(claimed_events) == 1
    ex = db.get(Execution, execution_id)
    assert ex.status == ExecutionStatus.CLAIMED.value  # 非终态
    db.close()


# =====================================================================
# 3. Idempotency-Key：并发同请求 / 异请求 409 / 数据库约束
# =====================================================================


def test_idempotency_concurrent_same_request_single_row(pg_env):
    engine, SessionLocal = pg_env
    key = _unique_key()
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    barrier = threading.Barrier(3)
    results: list[int] = []
    errors: list[BaseException] = []

    def _create() -> None:
        s = factory()
        try:
            barrier.wait(timeout=15)
            ex = create_execution(
                s, execution_type="system.ping", payload={},
                idempotency_key=key, clock=FixedClock(FIXED_NOW),
            )
            s.commit()
            results.append(ex.id)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            s.close()

    threads = [threading.Thread(target=_create) for _ in range(2)]
    for t in threads:
        t.start()
    barrier.wait(timeout=15)
    for t in threads:
        t.join(timeout=20)
    assert not errors, f"并发创建异常: {errors}"
    # 两个并发请求都成功且拿到同一个 execution（一个创建、一个 replay）
    assert len(results) == 2 and results[0] == results[1]

    db = SessionLocal()
    rows = db.query(Execution).filter(Execution.idempotency_key == key).all()
    assert len(rows) == 1  # 数据库只落一行
    db.close()


def test_idempotency_same_key_different_payload_conflict(pg_env):
    _, SessionLocal = pg_env
    _register_type("f6.pg.echo", payload_fields={"echo"})
    try:
        db = SessionLocal()
        key = _unique_key()
        first = create_execution(
            db, execution_type="f6.pg.echo", payload={"echo": "a"},
            idempotency_key=key, clock=FixedClock(FIXED_NOW),
        )
        db.commit()
        # 同 key 不同请求 → 稳定 409（服务层裁决）
        with pytest.raises(IdempotencyConflictError):
            create_execution(
                db, execution_type="f6.pg.echo", payload={"echo": "b"},
                idempotency_key=key, clock=FixedClock(FIXED_NOW),
            )
        db.rollback()
        rows = db.query(Execution).filter(Execution.idempotency_key == key).all()
        assert len(rows) == 1 and rows[0].id == first.id
        db.close()
    finally:
        _unregister_type("f6.pg.echo")


def test_idempotency_db_constraint_holds_after_restart(pg_env):
    """重启/新进程（全新 engine 与连接池）后仍由数据库约束保证。"""
    _, SessionLocal = pg_env
    _register_type("f6.pg.echo", payload_fields={"echo"})
    try:
        db = SessionLocal()
        key = _unique_key()
        create_execution(
            db, execution_type="f6.pg.echo", payload={"echo": "same"},
            idempotency_key=key, clock=FixedClock(FIXED_NOW),
        )
        db.commit()
        db.close()

        # 模拟新进程：全新 engine + 全新连接
        engine2 = create_engine(TEST_URL, pool_pre_ping=True)
        Session2 = sessionmaker(autocommit=False, autoflush=False, bind=engine2)
        db2 = Session2()
        # 同 key 同请求 → 重放（返回既有 execution，不重复创建）
        replayed = create_execution(
            db2, execution_type="f6.pg.echo", payload={"echo": "same"},
            idempotency_key=key, clock=FixedClock(FIXED_NOW),
        )
        db2.commit()
        assert replayed.id is not None
        rows = db2.query(Execution).filter(Execution.idempotency_key == key).all()
        assert len(rows) == 1
        # 同 key 异请求 → 409（数据库约束保证：唯一索引裁决路径）
        with pytest.raises(IdempotencyConflictError):
            create_execution(
                db2, execution_type="f6.pg.echo", payload={"echo": "other"},
                idempotency_key=key, clock=FixedClock(FIXED_NOW),
            )
        db2.close()
        engine2.dispose()
    finally:
        _unregister_type("f6.pg.echo")


# =====================================================================
# 4. Lease 与 stale worker：中断回收 + 幽灵写入全拒
# =====================================================================


def test_lease_recovery_stale_worker_all_writes_rejected(pg_env):
    engine, SessionLocal = pg_env
    db = SessionLocal()
    execution = _make(db)
    _enqueue(db, execution)
    clock = FakeClock()
    claimed = _claim(db, worker_id="crashy", clock=clock)
    assert claimed is not None
    execution, attempt = claimed
    transition_to(db, execution.id, ExecutionStatus.RUNNING, clock=clock)

    # 模拟 Worker 中断：不 finish，推进时钟过 lease
    clock.advance(61)
    assert recover_expired(db, clock=clock) == 1
    db.refresh(execution)
    assert execution.status == ExecutionStatus.QUEUED.value
    db.refresh(attempt)
    assert attempt.status == AttemptStatus.ABORTED.value  # 审计保留

    event_count_before = db.query(ExecutionEvent).count()

    # stale worker 的 renew → 拒绝（租约已失）
    with pytest.raises(ExecutionClaimLostError):
        renew_lease(db, execution, attempt, clock=clock, lease_ttl_seconds=60)

    # stale worker 的 finish → 拒绝（幽灵收尾绝不写入终态）
    ghost = ExecutionWorker(
        sessionmaker(autocommit=False, autoflush=False, bind=engine),
        worker_id="crashy", clock=clock, lease_ttl_seconds=60,
    )
    with pytest.raises(ExecutionClaimLostError):
        ghost._finish_attempt(
            db, attempt, AttemptStatus.SUCCEEDED, clock.now(),
            result_text="{}",
        )

    # stale worker 的 step 写入 → 拒绝（条件 UPDATE/租约断言失败）
    with pytest.raises(ExecutionClaimLostError):
        run_execution_steps(db, execution, attempt, clock=clock)

    # 以上全部拒绝路径不产生任何事件/终态/重复执行记录
    db.rollback()
    assert db.query(ExecutionEvent).count() == event_count_before
    db.refresh(execution)
    assert execution.status == ExecutionStatus.QUEUED.value
    assert db.query(Execution).filter(Execution.id == execution.id).count() == 1

    # 新 worker 可接管：attempt_number 递增，旧 attempt 保留
    clock.advance(1)
    claimed2 = _claim(db, worker_id="w2", clock=clock)
    assert claimed2 is not None
    _, attempt2 = claimed2
    assert attempt2.attempt_number == attempt.attempt_number + 1
    db.close()


# =====================================================================
# 5. 竞争裁决：至多一个合法终态
# =====================================================================


def test_cancel_success_race_single_terminal(pg_env):
    """handler 执行期间 cancel 到达 → 完成胜出（条件 UPDATE 单赢家）。"""
    engine, SessionLocal = pg_env
    clock = FakeClock()

    def _cancel_during(input_value):
        # 工具执行中 cancel 请求到达：CANCEL_REQUESTED 由 CAS 置位，
        # 但完成路径随后用另一 CAS 落到 SUCCEEDED —— 单赢家、单终态。
        # 关键：取消用【独立 session】（生产语义 = HTTP/外部连接），
        # 绝不共用测试的 db —— 共享会话会命中 identity map 的陈旧
        # 状态（enqueue 时的 QUEUED），CAS 读到旧值产生伪竞态。
        cancel_session = SessionLocal()
        try:
            request_cancel(cancel_session, execution.id, clock=clock)
        finally:
            cancel_session.close()
        return {"done": True}

    _register_type(
        "f6.pg.race_cancel",
        steps=(StepSpec(step_key="run", tool_name="f6.pg.race_cancel_tool"),),
    )
    _register_tool(
        "f6.pg.race_cancel_tool",
        handler=_cancel_during,
    )
    try:
        db = SessionLocal()
        execution = _make(db, execution_type="f6.pg.race_cancel")
        _enqueue(db, execution)
        worker = ExecutionWorker(
            sessionmaker(autocommit=False, autoflush=False, bind=db.get_bind()),
            worker_id="w1", clock=clock,
        )
        stats = worker.run_once()
        assert stats["succeeded"] == 1
        db.refresh(execution)
        assert execution.status == ExecutionStatus.SUCCEEDED.value  # 完成胜出
        attempt = (
            db.query(ExecutionAttempt)
            .filter(ExecutionAttempt.execution_id == execution.id)
            .one()
        )
        assert attempt.status == AttemptStatus.SUCCEEDED.value
        db.close()
    finally:
        _unregister_type("f6.pg.race_cancel")
        _unregister_tool("f6.pg.race_cancel_tool")


def test_timeout_success_race_single_terminal(pg_env):
    """timeout 与 success 竞争：两条顺序都只能有一个终态。"""
    _, SessionLocal = pg_env
    _register_type("f6.pg.slow", timeout_seconds=10)
    _register_tool(
        "f6.pg.slow_tool",
        handler=lambda input_value: {"ok": True},
    )
    try:
        db = SessionLocal()
        clock = FakeClock()

        # 顺序 A：sweeper 先裁决 → TIMED_OUT，worker 之后无法再写成功
        ex_a = _make(db, execution_type="f6.pg.slow")
        _enqueue(db, ex_a)
        claimed = _claim(db, worker_id="w1", clock=clock)
        assert claimed is not None
        transition_to(db, ex_a.id, ExecutionStatus.RUNNING, clock=clock)
        clock.advance(11)
        assert check_timeouts(db, clock=clock) == 1
        db.refresh(ex_a)
        assert ex_a.status == ExecutionStatus.TIMED_OUT.value
        attempt_a = (
            db.query(ExecutionAttempt)
            .filter(ExecutionAttempt.execution_id == ex_a.id)
            .one()
        )
        assert attempt_a.status == AttemptStatus.ABORTED.value

        # 顺序 B：worker 已完成 → SUCCEEDED，sweeper 后到无操作
        ex_b = _make(db, execution_type="f6.pg.slow")
        _enqueue(db, ex_b)
        claimed2 = _claim(db, worker_id="w2", clock=clock)
        assert claimed2 is not None
        transition_to(db, ex_b.id, ExecutionStatus.RUNNING, clock=clock)
        transition_to(db, ex_b.id, ExecutionStatus.SUCCEEDED, clock=clock)
        assert check_timeouts(db, clock=clock) == 0
        db.refresh(ex_b)
        assert ex_b.status == ExecutionStatus.SUCCEEDED.value
        db.close()
    finally:
        _unregister_type("f6.pg.slow")
        _unregister_tool("f6.pg.slow_tool")


def test_misfire_skip_fail_vs_claim(pg_env):
    """misfire SKIP/FAIL 与 claim 竞争：至多一个裁决，绝不双赢。"""
    _, SessionLocal = pg_env
    _register_type("f6.pg.skip", misfire_policy="SKIP", misfire_grace_seconds=300.0)
    _register_type("f6.pg.fail", misfire_policy="FAIL", misfire_grace_seconds=300.0)
    _register_type(
        "f6.pg.runnow", misfire_policy="RUN_IMMEDIATELY",
        misfire_grace_seconds=300.0,
    )
    _register_tool("f6.pg.pt", handler=lambda input_value: {"ok": True})
    try:
        db = SessionLocal()
        clock = FakeClock()
        past = FIXED_NOW - timedelta(seconds=1000)

        # SKIP：两个 worker 先后 claim 一个 misfired 执行 → 无 claim、单终态 CANCELLED
        ex = _make(db, execution_type="f6.pg.skip", run_at=past)
        _enqueue(db, ex)
        assert _claim(db, worker_id="w-1", clock=clock) is None
        assert _claim(db, worker_id="w-2", clock=clock) is None
        db.refresh(ex)
        assert ex.status == ExecutionStatus.CANCELLED.value
        assert db.query(ExecutionAttempt).count() == 0
        events = db.query(ExecutionEvent).filter(
            ExecutionEvent.execution_id == ex.id
        ).all()
        types = [e.event_type for e in events]
        assert types.count("MISFIRE_DETECTED") == 1
        assert types.count("MISFIRE_SKIPPED") == 1
        assert "EXECUTION_CLAIMED" not in types

        # FAIL：无 claim、单终态 FAILED + 稳定错误码
        ex2 = _make(db, execution_type="f6.pg.fail", run_at=past)
        _enqueue(db, ex2)
        assert _claim(db, worker_id="w-3", clock=clock) is None
        db.refresh(ex2)
        assert ex2.status == ExecutionStatus.FAILED.value
        assert ex2.last_error_code == "EXEC_MISFIRE_EXPIRED"

        # RUN_IMMEDIATELY：两个 worker 竞争 → 一个 claim 一套事件
        ex3 = _make(db, execution_type="f6.pg.runnow", run_at=past)
        _enqueue(db, ex3)
        assert _claim(db, worker_id="w-4", clock=clock) is not None
        assert _claim(db, worker_id="w-5", clock=clock) is None
        events3 = db.query(ExecutionEvent).filter(
            ExecutionEvent.execution_id == ex3.id
        ).all()
        types3 = [e.event_type for e in events3]
        assert types3.count("MISFIRE_DETECTED") == 1
        assert types3.count("MISFIRE_RUN_IMMEDIATELY") == 1
        assert types3.count("EXECUTION_CLAIMED") == 1
        db.close()
    finally:
        _unregister_type("f6.pg.skip")
        _unregister_type("f6.pg.fail")
        _unregister_type("f6.pg.runnow")
        _unregister_tool("f6.pg.pt")


def test_confirmation_same_action_idempotent_and_irreversible(pg_env):
    """grant/reject 同 action 幂等；不同 action 不可翻转（409）。

    流程沿 6E 权威模式：确认门 PENDING → 可重试失败 → grant 后
    Worker 越退避窗口再跑 → 门放行执行；reject 后稳定失败绝不执行。
    """
    _, SessionLocal = pg_env
    calls = {"n": 0}
    _register_type(
        "f6.pg.risk",
        payload_fields={"message"},
        max_attempts=3,
        steps=(StepSpec(step_key="risky", tool_name="f6.pg.risky_tool"),),
    )
    _register_tool(
        "f6.pg.risky_tool",
        handler=lambda input_value: calls.update(n=calls["n"] + 1) or {"done": True},
        destructive=True,
        requires_confirmation=True,
        replay_safe=False,
        retryable=False,
    )
    try:
        db = SessionLocal()
        clock = FakeClock()
        worker = ExecutionWorker(
            sessionmaker(autocommit=False, autoflush=False, bind=db.get_bind()),
            worker_id="w1", clock=clock,
        )

        # ---- 路径 A：grant ----
        ex = create_execution(
            db, execution_type="f6.pg.risk", payload={"message": "hi"},
            idempotency_key=_unique_key(), clock=clock,
        )
        db.commit()
        _enqueue(db, ex)
        # 首次运行：destructive 未确认 → handler 绝不执行，PENDING 门可重试
        stats = worker.run_once()
        assert stats["retried"] == 1  # EXEC_CONFIRMATION_REQUIRED 可重试
        assert calls["n"] == 0  # handler 从未被调用
        db.refresh(ex)
        assert ex.status == ExecutionStatus.QUEUED.value  # 退避后回队
        step = db.query(ExecutionStep).filter(
            ExecutionStep.execution_id == ex.id,
            ExecutionStep.step_key == "risky",
        ).one()
        assert step.confirmation_status == "PENDING"

        # grant 同 action 幂等（同 actor 第二次 → 仍 GRANTED，不冲突）
        g1 = grant_confirmation(
            db, ex.id, "risky", actor_type="user", actor_id="alice", clock=clock,
        )
        assert g1.confirmation_status == "GRANTED"
        g2 = grant_confirmation(
            db, ex.id, "risky", actor_type="user", actor_id="alice", clock=clock,
        )
        assert g2.confirmation_status == "GRANTED" and g2.id == g1.id
        # grant 后 reject → 不可翻转（409 EXEC_STEP_CONFLICT）
        with pytest.raises(StepConflictError):
            reject_confirmation(
                db, ex.id, "risky", actor_type="user", actor_id="alice",
                clock=clock,
            )
        db.rollback()
        # Worker 越退避窗口重跑：门放行 → destructive 恰好执行一次 → SUCCEEDED
        clock.advance(10)
        stats = worker.run_once()
        assert stats["succeeded"] == 1
        assert calls["n"] == 1
        db.refresh(ex)
        assert ex.status == ExecutionStatus.SUCCEEDED.value

        # ---- 路径 B：reject（6C 权威流程：执行前拒绝） ----
        ex2 = create_execution(
            db, execution_type="f6.pg.risk", payload={"message": "hi2"},
            idempotency_key=_unique_key(), clock=clock,
        )
        db.commit()
        _enqueue(db, ex2)
        # 首次执行前拒绝：step 物化（PENDING）→ REJECTED，绝不产生 RETRY
        r1 = reject_confirmation(
            db, ex2.id, "risky", actor_type="user", actor_id="bob", clock=clock,
        )
        assert r1.confirmation_status == "REJECTED"
        # reject 同 action 幂等（同 actor 第二次 → 仍 REJECTED，不冲突）
        r2 = reject_confirmation(
            db, ex2.id, "risky", actor_type="user", actor_id="bob", clock=clock,
        )
        assert r2.confirmation_status == "REJECTED" and r2.id == r1.id
        # reject 后 grant → 不可翻转（409 EXEC_STEP_CONFLICT）
        with pytest.raises(StepConflictError):
            grant_confirmation(
                db, ex2.id, "risky", actor_type="user", actor_id="bob",
                clock=clock,
            )
        db.rollback()
        # REJECTED 后 Worker 执行：门稳定失败，绝不执行 handler、绝不重试
        calls_before = calls["n"]
        clock.advance(10)
        stats = worker.run_once()
        assert stats["failed"] == 1
        assert calls["n"] == calls_before  # destructive handler 未被调用
        db.refresh(ex2)
        assert ex2.status == ExecutionStatus.FAILED.value
        assert ex2.last_error_code == "EXEC_CONFIRMATION_REJECTED"
        ex2_events = [
            e.event_type
            for e in db.query(ExecutionEvent)
            .filter(ExecutionEvent.execution_id == ex2.id)
            .all()
        ]
        assert "RETRY_SCHEDULED" not in ex2_events  # 不重试
        db.close()
    finally:
        _unregister_type("f6.pg.risk")
        _unregister_tool("f6.pg.risky_tool")


# =====================================================================
# 6. steps/events：约束、单调、回滚、脱敏、安全门
# =====================================================================


def test_steps_events_unique_and_monotonic_sequence(pg_env):
    _, SessionLocal = pg_env
    db = SessionLocal()
    execution = _make(db)
    _enqueue(db, execution)
    claimed = _claim(db, worker_id="w1")
    assert claimed is not None
    execution, attempt = claimed
    transition_to(db, execution.id, ExecutionStatus.RUNNING)

    # sequence 单调递增（1..n，无空洞）
    for i in range(1, 4):
        append_event(
            db, execution_id=execution.id, attempt_id=attempt.id,
            event_type="STEP_STARTED", actor_type="worker",
            actor_id="w1", payload={"step_index": i}, clock=FixedClock(FIXED_NOW),
        )
    db.commit()
    events = list_events(db, execution.id)
    seqs = [e.sequence_number for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seqs == list(range(1, len(seqs) + 1))  # 从 1 起、无空洞

    # 唯一约束：直接插入重复 sequence → IntegrityError（数据库级裁决）
    dup_ev = ExecutionEvent(
        execution_id=execution.id,
        attempt_id=attempt.id,
        event_type="STEP_STARTED",
        sequence_number=events[0].sequence_number,  # 与已有事件重复
        actor_type="worker",
        actor_id="w1",
        correlation_id=None,
        payload=None,
        created_at=FIXED_NOW,
    )
    db.add(dup_ev)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()

    # 唯一约束（步骤行）：重复 step_index → IntegrityError（数据库级）
    dup_step = ExecutionStep(
        execution_id=execution.id,
        attempt_id=attempt.id,
        step_key="s1",
        step_index=1,
        tool_name="system.ping",
        status="SUCCEEDED",
        input_json="{}",
        input_hash="h" * 64,
        requires_confirmation=False,
        started_at=FIXED_NOW,
    )
    db.add(dup_step)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()


def test_event_and_status_rollback_together(pg_env):
    """事务回滚时：事件与状态变更一起消失（同事务原子）。

    append_event 设计上不提交（"调用方把本事件与状态变更放在同一事务内
    提交"）——worker 的状态+事件原子写入依赖这一点。这里在单个未提交
    事务中同时做状态变更与事件写入，回滚后两者一并消失，提交后一并可见。
    """
    from sqlalchemy import update

    _, SessionLocal = pg_env
    db = SessionLocal()
    execution = _make(db)
    _enqueue(db, execution)
    execution_id = execution.id

    # 回滚路径：状态 + 事件同一事务 → 一起消失
    db.execute(
        update(Execution)
        .where(Execution.id == execution_id)
        .values(status=ExecutionStatus.RUNNING.value)
    )
    append_event(
        db, execution_id=execution_id,
        event_type="EXECUTION_STARTED", actor_type="worker", actor_id="w1",
        clock=FixedClock(FIXED_NOW),
    )
    db.rollback()  # 模拟中途失败回滚

    db2 = SessionLocal()
    ex2 = db2.get(Execution, execution_id)
    assert ex2.status == ExecutionStatus.QUEUED.value  # 状态回滚到入队态
    events2 = db2.query(ExecutionEvent).filter(
        ExecutionEvent.execution_id == execution_id
    ).all()
    assert all(e.event_type != "EXECUTION_STARTED" for e in events2)  # 事件同消
    db2.close()

    # 提交路径：同一事务 → 一起可见
    db.execute(
        update(Execution)
        .where(Execution.id == execution_id)
        .values(status=ExecutionStatus.RUNNING.value)
    )
    append_event(
        db, execution_id=execution_id,
        event_type="EXECUTION_STARTED", actor_type="worker", actor_id="w1",
        clock=FixedClock(FIXED_NOW),
    )
    db.commit()
    db3 = SessionLocal()
    ex3 = db3.get(Execution, execution_id)
    assert ex3.status == ExecutionStatus.RUNNING.value
    events3 = db3.query(ExecutionEvent).filter(
        ExecutionEvent.execution_id == execution_id
    ).all()
    assert any(e.event_type == "EXECUTION_STARTED" for e in events3)
    db3.close()
    db.close()


def test_sensitive_fields_redacted_on_disk(pg_env):
    """敏感字段绝不原样落盘：payload 白名单拒绝；事件 payload 脱敏。"""
    _, SessionLocal = pg_env
    _register_type("f6.pg.redact", payload_fields={"echo", "message"})
    try:
        db = SessionLocal()
        # 白名单外字段 → 拒绝（绝不静默丢弃）
        from app.services.execution_errors import ExecutionPayloadError

        with pytest.raises(ExecutionPayloadError):
            create_execution(
                db, execution_type="f6.pg.redact",
                payload={"echo": "x", "api_key": "sk-leak-me"},
                idempotency_key=_unique_key(), clock=FixedClock(FIXED_NOW),
            )
        db.rollback()

        # 合法字段落盘：只含白名单字段，无多余键
        ex = create_execution(
            db, execution_type="f6.pg.redact",
            payload={"echo": "hello", "message": "world"},
            idempotency_key=_unique_key(), clock=FixedClock(FIXED_NOW),
        )
        db.commit()
        stored = json.loads(db.get(Execution, ex.id).payload)
        assert set(stored.keys()) == {"echo", "message"}
        assert "hello" in stored["echo"]

        # 事件 payload 脱敏：敏感字段名 → "<redacted>"
        append_event(
            db, execution_id=ex.id,
            event_type="STEP_STARTED", actor_type="worker",
            actor_id="w1",
            payload={"message": "hi", "api_key": "sk-leak-me"},
            clock=FixedClock(FIXED_NOW),
        )
        db.commit()
        ev = (
            db.query(ExecutionEvent)
            .filter(
                ExecutionEvent.execution_id == ex.id,
                ExecutionEvent.event_type == "STEP_STARTED",
            )
            .one()
        )
        assert ev.payload is not None
        assert "sk-leak-me" not in ev.payload  # 原始值绝不落盘
        ev_payload = json.loads(ev.payload)
        assert ev_payload["message"] == "hi"
        # sanitize_result_value 把敏感键名本身替换为 "<redacted>"（键+值双替换）
        assert "api_key" not in ev_payload
        assert ev_payload.get("<redacted>") == "<redacted>"
        db.close()
    finally:
        _unregister_type("f6.pg.redact")
