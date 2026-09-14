"""Phase 3 强化审查：故障注入、幂等、日志脱敏、迁移带数据往返、Worker 退出码。

覆盖审查清单中要求新增的故障注入点：
- Notification 创建后提交失败（SQLAlchemyError 与普通异常两条路径）；
- 唯一约束冲突（幂等成功 / 无既有记录则 FAILED）；
- 外键约束（Notification 插入拒绝）；
- lease 恢复后不产生重复通知；
- 单批中第一条失败、第二条仍可处理（含领取阶段失败）；
- 迁移带数据往返（003 → head → downgrade 003 → re-upgrade）；
- Worker 重复执行；
- Worker 日志脱敏与 --once 退出码。
"""

import logging
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.clock import FixedClock
from app.core.database import Base
from app.models.reminder import Notification, Reminder, ReminderStatus
from app.models.task import Task, TaskStatus
from app.workers import reminder_worker
from tests.conftest import FIXED_NOW
from tests.test_reminders import (
    _clock,
    _notifications,
    _reminders,
    confirm,
    make_task,
    run_once,
)

UTC = timezone.utc
TASK_API = "/api/tasks"
BACKEND_DIR = Path(__file__).resolve().parents[1]
DUE_TODAY = "2026-08-11T12:00:00+08:00"  # = 04:00Z（FIXED_NOW 当天到期）


# ---------- 一、投递幂等：唯一约束冲突 ----------


def test_delivery_unique_conflict_is_idempotent_success(client, worker_env):
    """Notification 已存在（如另一 Worker 已投递）：唯一冲突 → 幂等成功，不无限失败。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    reminder = _reminders(client)[0]

    engine, SessionLocal = worker_env
    db = SessionLocal()
    db.add(
        Notification(
            reminder_id=reminder["id"],
            task_id=task["id"],
            type="REMINDER_DUE",
            title="已投递",
            body="另一 Worker 已投递",
        )
    )
    db.commit()
    db.close()

    # 首次冲突轮询：识别为已投递 → DELIVERED，attempt 只 +1（领取时）
    stats = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.delivered == 1
    assert stats.failed == 0
    reminders = _reminders(client)
    assert reminders[0]["status"] == "DELIVERED"
    assert reminders[0]["attempt_count"] == 1
    assert reminders[0]["last_error_code"] is None

    # 后续轮询不再扫描 → 不会无限重试
    stats2 = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats2.scanned == 0
    assert len(_notifications(client)) == 1  # 没有重复通知


def test_delivery_unique_conflict_without_existing_marks_failed(client, worker_env, monkeypatch):
    """唯一冲突但查不到既有 Notification（异常历史/损坏状态）：FAILED 终态而非无限重试。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])

    from sqlalchemy.orm import Session as SaSession

    real_commit = SaSession.commit

    def conflict_commit(self, *args, **kwargs):
        raise IntegrityError("INSERT INTO notifications", {}, Exception("UNIQUE constraint failed"))

    # 只让投递提交（第 2 次）抛唯一冲突；领取提交正常
    calls = {"n": 0}

    def flaky_commit(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return conflict_commit(self, *args, **kwargs)
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "commit", flaky_commit)
    stats = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    monkeypatch.undo()

    assert stats.failed == 1
    reminders = _reminders(client)
    assert reminders[0]["status"] == "FAILED"
    assert reminders[0]["last_error_code"] == "DELIVERY_UNIQUE_CONFLICT"

    # 终态：后续轮询不触碰
    stats2 = run_once(worker_env, _clock(offset_hours=2), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats2.scanned == 0
    assert _notifications(client) == []


# ---------- 二、投递提交失败：真实 SQLAlchemyError 路径 ----------


def test_delivery_commit_sqlalchemy_error_is_retryable(client, worker_env, monkeypatch):
    """投递提交失败（SQLAlchemyError，生产真实类型）→ claimed 可重试，恢复后恰好一次。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])

    from sqlalchemy.orm import Session as SaSession

    real_commit = SaSession.commit
    calls = {"n": 0}

    def flaky_commit(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # 投递提交失败（模拟磁盘/锁错误）
            raise OperationalError("UPDATE notifications", {}, RuntimeError("database is locked"))
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "commit", flaky_commit)
    stats = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    monkeypatch.undo()

    # SQLAlchemyError 路径：返回 claimed（不计数 failed），保持 CLAIMED 等 lease
    assert stats.claimed == 1
    assert stats.failed == 0
    assert stats.delivered == 0
    reminders = _reminders(client)
    assert reminders[0]["status"] == "CLAIMED"
    assert reminders[0]["attempt_count"] == 1
    assert _notifications(client) == []  # 提交失败 = Notification 未落库

    # lease 过期后恢复：恰好一次通知
    engine, SessionLocal = worker_env
    db = SessionLocal()
    # raw text() SQL 绑定必须用 naive datetime：aware 值会被 sqlite3 存成
    # "2020-01-01 00:00:00+00:00" 字符串，恢复轮询时 SQLAlchemy 解析会抛 TypeError
    db.execute(
        text("UPDATE reminders SET lease_expires_at=:past WHERE task_id=:tid"),
        {"past": datetime(2020, 1, 1, 0, 0), "tid": task["id"]},
    )
    db.commit()
    db.close()
    stats2 = run_once(worker_env, _clock(offset_hours=2), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats2.delivered == 1
    assert len(_notifications(client)) == 1


# ---------- 三、外键约束 ----------


def test_notification_fk_violation_rejected(client, worker_env):
    """Notification 引用不存在的 reminder/task：外键拒绝（IntegrityError）。"""
    engine, SessionLocal = worker_env
    db = SessionLocal()
    db.add(
        Notification(
            reminder_id=99999,
            task_id=99999,
            type="REMINDER_DUE",
            title="x",
            body="y",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()
    assert _notifications(client) == []


def test_cascade_removes_history_no_orphans(client, worker_env, tmp_path):
    """删除 Task：Reminder 与 Notification 级联删除，不留孤儿。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert len(_notifications(client)) == 1

    con = sqlite3.connect(tmp_path / "test_tasks.db")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
    con.commit()
    r = con.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
    n = con.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    con.close()
    assert r == 0 and n == 0


# ---------- 四、per-item 隔离：领取阶段失败 ----------


def test_claim_failure_isolated_second_still_delivered(client, worker_env, monkeypatch):
    """批次第一条领取提交失败 → 回滚保持 PENDING（attempt 不变），第二条仍正常投递。"""
    for i in range(2):
        task = make_task(client, due_at=DUE_TODAY, title=f"claim-{i}")
        confirm(client, task["id"])

    from sqlalchemy.orm import Session as SaSession

    real_commit = SaSession.commit
    calls = {"n": 0}

    def flaky_commit(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # 第一条的领取提交失败
            raise OperationalError("UPDATE reminders", {}, RuntimeError("locked"))
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "commit", flaky_commit)
    stats = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    monkeypatch.undo()

    assert stats.failed == 1
    assert stats.delivered == 1
    reminders = _reminders(client)
    statuses = sorted(r["status"] for r in reminders)
    assert statuses == ["DELIVERED", "PENDING"]  # 第一条未领取（attempt 未变），第二条成功
    attempts = sorted(r["attempt_count"] for r in reminders)
    assert attempts == [0, 1]
    assert len(_notifications(client)) == 1


def test_batch_order_deterministic_by_id(client, worker_env, monkeypatch):
    """同一 remind_at 的多条提醒按 id 升序处理（确定性，测试故障注入依赖该顺序）。"""
    from app.services import reminder_service as rs

    for i in range(3):
        task = make_task(client, due_at=DUE_TODAY, title=f"order-{i}")
        confirm(client, task["id"])
    reminders = _reminders(client)
    ids = [r["id"] for r in reminders]
    assert len(set(ids)) == 3

    real_deliver = rs._deliver
    seen = []

    def recording_deliver(db, reminder, now):
        seen.append(reminder.id)
        return real_deliver(db, reminder, now)

    monkeypatch.setattr(rs, "_deliver", recording_deliver)
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    monkeypatch.undo()
    assert seen == sorted(ids), "同 remind_at 必须按 id 升序投递"


# ---------- 五、状态机边界（文档化行为固定） ----------


def test_postpone_after_confirm_without_due_at_creates_no_reminder(client):
    """CONFIRMED 无 Reminder 的历史数据：postpone 不创建新提醒（一次性提醒语义，文档化）。"""
    task = make_task(client)  # 无 due_at → confirm 不创建 Reminder
    confirm(client, task["id"])
    assert _reminders(client) == []

    client.patch(f"{TASK_API}/{task['id']}/postpone", json={"due_at": "2026-08-30T09:00:00+08:00"})
    assert _reminders(client) == []  # 只同步已存在的 PENDING；不新造提醒


def test_mark_read_uses_injected_clock(client, worker_env):
    """标记已读使用可注入时钟：read_at 精确等于 FIXED_NOW（确定性，不依赖真实时间）。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    nid = _notifications(client)[0]["id"]

    resp = client.post(f"/api/notifications/{nid}/read")
    assert resp.status_code == 200
    assert resp.json()["read_at"] == "2026-08-11T04:00:00Z"  # FIXED_NOW


def test_notification_body_bounded_by_title_length(client, worker_env):
    """Notification 正文由服务端生成：title 上限 200，body 长度有界。"""
    long_title = "t" * 200
    task = make_task(client, due_at=DUE_TODAY, title=long_title)
    confirm(client, task["id"])
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)

    n = _notifications(client)[0]
    assert len(n["title"]) <= 200 + len("任务提醒：")
    assert len(n["body"]) <= 250  # 200 字标题 + 固定文案的实测上界
    assert long_title in n["body"]


# ---------- 六、Session 回滚后可用性 ----------


def test_session_usable_after_rollback(tmp_path):
    """rollback 后的 Session 仍能正常查询、提交与关闭（清理路径健壮）。"""
    from sqlalchemy import event

    from app.core.database import _set_sqlite_pragma

    engine = create_engine(
        f"sqlite:///{tmp_path / 'sess.db'}", connect_args={"check_same_thread": False}
    )
    # 外键必须真实生效，否则 Reminder(task_id=99999) 不会触发 IntegrityError
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    db = SessionLocal()
    db.add(Reminder(task_id=99999, status="PENDING", remind_at=datetime(2026, 1, 1, tzinfo=UTC)))
    with pytest.raises(IntegrityError):
        db.commit()  # 外键失败
    db.rollback()

    # 回滚后同一 Session 可继续工作
    db.add(Task(title="after rollback", status=TaskStatus.DRAFT.value))
    db.commit()
    assert db.query(Task).count() == 1
    db.close()  # 关闭无异常


# ---------- 七、Worker 日志脱敏 ----------


def test_sanitize_error_message_strips_secrets_and_truncates(monkeypatch):
    """错误消息脱敏：数据库 URL / API Key 被移除，超长内容被截断。"""
    monkeypatch.setattr(reminder_worker.settings, "database_url", "sqlite:///secret/path.db")
    monkeypatch.setattr(reminder_worker.settings, "llm_api_key", "sk-secret-key-123")
    msg = (
        f"connection failed url={reminder_worker.settings.database_url} "
        f"key={reminder_worker.settings.llm_api_key} "
        + "u" * 2000
    )
    out = reminder_worker.sanitize_error_message(msg)
    assert "secret/path.db" not in out
    assert "sk-secret-key-123" not in out
    assert len(out) <= reminder_worker.MAX_LOG_MESSAGE_CHARS
    assert "<redacted>" in out


def test_worker_tick_error_log_is_sanitized(client, worker_env, monkeypatch, caplog):
    """_safe_tick 的异常日志：只记录类型 + 脱敏消息，不含密钥与完整长内容。"""
    monkeypatch.setattr(reminder_worker.settings, "database_url", "sqlite:///secret/path.db")
    monkeypatch.setattr(reminder_worker.settings, "llm_api_key", "sk-secret-key-123")

    def exploding(db, clock, **kwargs):
        raise RuntimeError(
            f"boom url={reminder_worker.settings.database_url} "
            f"key={reminder_worker.settings.llm_api_key} " + "s" * 1000
        )

    monkeypatch.setattr(reminder_worker, "process_due_reminders_once", exploding)
    engine, SessionLocal = worker_env
    db = SessionLocal()
    logger = logging.getLogger("reminder_worker")
    # alembic env.py 的 fileConfig(disable_existing_loggers=True) 会禁用此前创建的
    # 既有 logger（由其他测试文件的 command.upgrade 触发）；生产 Worker 进程从不
    # 经过 fileConfig，日志正常 —— 这里恢复 enabled 以隔离测试顺序污染。
    monkeypatch.setattr(logger, "disabled", False)
    with caplog.at_level(logging.ERROR, logger="reminder_worker"):
        reminder_worker._safe_tick(db)  # 不崩溃
    db.close()
    monkeypatch.undo()

    records = [r for r in caplog.records if r.name == "reminder_worker"]
    assert any("reminder_worker.error" in r.getMessage() for r in records)
    for r in records:
        assert "sk-secret-key-123" not in r.getMessage()
        assert "secret/path.db" not in r.getMessage()
        assert "s" * 1000 not in r.getMessage()  # 完整长内容未进日志


# ---------- 八、--once 退出码 ----------


def _worker_cmd(tmp_path, *extra, db_url=None):
    env = dict(os.environ)
    env["DATABASE_URL"] = db_url or f"sqlite:///{tmp_path / 'worker.db'}"
    return subprocess.run(
        [sys.executable, "-m", "app.workers.reminder_worker", *extra],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_once_success_exit_code_0(client, worker_env, tmp_path):
    """--once 成功处理（含投递）→ 退出码 0。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])

    # 复用 client 夹具的库（已建表 + 已有到期 Reminder）；空库无表会以退出码 1 失败
    result = _worker_cmd(tmp_path, "--once", db_url=f"sqlite:///{tmp_path / 'test_tasks.db'}")
    assert result.returncode == 0, f"stderr={result.stderr}"
    assert "stats=" in result.stdout


def test_once_bad_now_exit_code_1(tmp_path):
    """--once 带非法 --now → 退出码 1（启动配置失败与成功可区分）。"""
    result = _worker_cmd(tmp_path, "--once", "--now", "not-a-time")
    assert result.returncode == 1
    assert "startup_failed" in result.stderr


def test_once_bad_database_exit_code_1(tmp_path):
    """--once 数据库不可用 → 退出码 1，且日志不含数据库 URL。"""
    missing = tmp_path / "no_such_dir" / "worker.db"
    result = _worker_cmd(tmp_path, "--once", db_url=f"sqlite:///{missing}")
    assert result.returncode == 1
    assert str(missing) not in (result.stdout + result.stderr)  # 路径被脱敏


# ---------- 九、迁移带数据往返 ----------


def _alembic_cfg(db_file: Path) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def test_migration_003_to_head_roundtrip_preserves_data(tmp_path):
    """003（带真实样例数据）→ 005 head → downgrade 003 → re-upgrade：数据全程保留。"""
    db_file = tmp_path / "roundtrip3.db"
    cfg = _alembic_cfg(db_file)
    command.upgrade(cfg, "66405497a397")  # 003

    engine = create_engine(f"sqlite:///{db_file}")
    SessionLocal = sessionmaker(bind=engine)

    # 真实样例数据：多种状态的 Task + 对话消息 + SUCCEEDED Proposal
    s = SessionLocal()
    draft = Task(title="草稿任务", status=TaskStatus.DRAFT.value)
    confirmed = Task(title="已确认任务", status=TaskStatus.CONFIRMED.value, due_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC))
    done = Task(title="已完成任务", status=TaskStatus.DONE.value)
    s.add_all([draft, confirmed, done])
    s.commit()

    from app.models.conversation import Conversation, Message, MessageRole

    conv = Conversation(title="样例对话")
    s.add(conv)
    s.commit()
    s.add(Message(conversation_id=conv.id, role=MessageRole.USER.value, content="安排任务"))
    s.add(Message(conversation_id=conv.id, role=MessageRole.ASSISTANT.value, content="好的"))
    s.execute(
        text(
            "INSERT INTO task_proposals (conversation_id,user_message_id,assistant_message_id,"
            "task_id,action,arguments_json,explanation,status,created_at) VALUES "
            "(:cid,:uid,:aid,:tid,'create_task_draft','{}','explanation','SUCCEEDED',:ts)"
        ),
        {"cid": conv.id, "uid": 1, "aid": 2, "tid": draft.id,
         "ts": datetime(2026, 8, 11, 4, 0, 0)},
    )
    s.commit()
    s.close()

    # 升级到 head：新表出现，旧数据保留
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as c:
        titles = sorted(r[0] for r in c.execute(text("SELECT title FROM tasks")))
        assert titles == ["已完成任务", "已确认任务", "草稿任务"]
        statuses = set(r[0] for r in c.execute(text("SELECT status FROM tasks")))
        assert statuses == {"DRAFT", "CONFIRMED", "DONE"}
        assert c.execute(text("SELECT COUNT(*) FROM messages")).scalar() == 2
        assert c.execute(text("SELECT COUNT(*) FROM task_proposals")).scalar() == 1
        assert c.execute(text("SELECT action FROM task_proposals")).scalar() == "create_task_draft"
        assert c.execute(text("SELECT COUNT(*) FROM reminders")).scalar() == 0  # 新表空，迁移不依赖数据为空
        assert c.execute(text("SELECT COUNT(*) FROM notifications")).scalar() == 0

    # downgrade 到 003：新表移除、Proposal 新增列与索引移除、旧数据保留
    command.downgrade(cfg, "66405497a397")
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as c:
        tables = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
        assert "reminders" not in tables and "notifications" not in tables
        cols = [r[1] for r in c.execute(text("PRAGMA table_info(task_proposals)"))]
        assert "error_code" not in cols and "error_detail" not in cols
        idx = {r[0] for r in c.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))}
        assert "ix_task_proposals_status" not in idx  # 004 新增的索引已移除
        assert c.execute(text("SELECT COUNT(*) FROM tasks")).scalar() == 3
        assert c.execute(text("SELECT COUNT(*) FROM messages")).scalar() == 2

    # 再 upgrade 到 head：全部恢复
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM tasks")).scalar() == 3
        assert c.execute(text("SELECT COUNT(*) FROM messages")).scalar() == 2
        assert c.execute(text("SELECT action FROM task_proposals")).scalar() == "create_task_draft"


def test_migration_downgrade_with_rejected_rows_fails_loudly(tmp_path):
    """REJECTED 行（可空列含 NULL）存在时 downgrade 到 003 必须响亮失败，禁止静默数据损坏。"""
    db_file = tmp_path / "rejected.db"
    cfg = _alembic_cfg(db_file)
    command.upgrade(cfg, "a1b2c3d4e5f6")  # 004：REJECTED 审计列已存在

    engine = create_engine(f"sqlite:///{db_file}")
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    s.add(Task(title="legacy", status=TaskStatus.DRAFT.value))
    s.commit()
    s.execute(
        text(
            "INSERT INTO task_proposals (conversation_id,user_message_id,assistant_message_id,"
            "task_id,action,arguments_json,explanation,status,created_at,error_code,error_detail) "
            "VALUES (1,1,NULL,NULL,NULL,NULL,NULL,'REJECTED','2026-08-11 04:00:00',"
            "'PROMPT_INJECTION','suspicious')"
        )
    )
    s.commit()
    s.close()

    # 003 的 NOT NULL 约束无法容纳 REJECTED 行 → 迁移失败而不是静默丢数据
    with pytest.raises(IntegrityError, match="NOT NULL constraint failed"):
        command.downgrade(cfg, "66405497a397")


def test_create_all_database_does_not_silently_skip_migrations(tmp_path):
    """create_all 建的库没有 alembic_version：alembic upgrade 必须响亮失败而非跳过。"""
    db_file = tmp_path / "create_all.db"
    engine = create_engine(f"sqlite:///{db_file}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)  # 模拟开发模式建表（无 alembic_version）

    with pytest.raises(OperationalError, match="already exists"):
        command.upgrade(_alembic_cfg(db_file), "head")

    # 对齐指引存在：stamp head 后 upgrade 幂等成功
    command.stamp(_alembic_cfg(db_file), "head")
    command.upgrade(_alembic_cfg(db_file), "head")
    con = sqlite3.connect(db_file)
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "8c9d0e1f2a3b4"
    con.close()


# ---------- 十、unread count 一致性（数据库状态） ----------


def test_unread_count_matches_list_after_bulk_reads(client, worker_env):
    """批量标记已读后：unread count 与未读列表在数据库状态下保持一致。"""
    for i in range(3):
        task = make_task(client, due_at=DUE_TODAY, title=f"unread-{i}")
        confirm(client, task["id"])
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)

    assert client.get("/api/notifications/unread-count").json()["count"] == 3
    for n in _notifications(client):
        assert client.post(f"/api/notifications/{n['id']}/read").status_code == 200
    assert client.get("/api/notifications/unread-count").json()["count"] == 0
    assert _notifications(client, unread_only=True) == []
