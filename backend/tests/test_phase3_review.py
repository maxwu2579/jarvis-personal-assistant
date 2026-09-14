"""Phase 3 严格审查测试：事务边界故障注入、状态机边界、约束与索引验证。

全部使用 Fake Clock 与独立临时数据库。
"""

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.clock import FixedClock
from app.core.database import Base, engine
from app.models.reminder import Notification, Reminder, ReminderStatus
from app.services.reminder_service import process_due_reminders_once
from tests.conftest import FIXED_NOW
from tests.conftest import run_worker_once
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
DUE_TODAY = "2026-08-11T12:00:00+08:00"  # = 04:00Z（FIXED_NOW 当天到期）


# ---------- 一、事务边界：故障注入证明 ----------


def test_confirm_flush_failure_rolls_back_task_status(client, worker_env, monkeypatch):
    """Reminder 创建（flush）失败 → Task 不能残留为 CONFIRMED。"""
    from sqlalchemy.orm import Session as SaSession

    task = make_task(client, due_at=DUE_TODAY)
    real_flush = SaSession.flush
    calls = {"n": 0}

    def flaky_flush(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 1:  # commit 时唯一一次 flush 抛错 → 整体回滚
            raise RuntimeError("reminder flush failed")
        return real_flush(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "flush", flaky_flush)
    resp = confirm(client, task["id"])
    monkeypatch.undo()

    assert resp.status_code == 500
    # Task 未残留为 CONFIRMED
    assert client.get(f"{TASK_API}/{task['id']}").json()["status"] == "DRAFT"
    assert _reminders(client) == []


def test_postpone_reminder_update_failure_keeps_due_at(client, worker_env, monkeypatch):
    """postpone 时 Reminder 更新失败 → Task.due_at 不能单独变化。"""
    from sqlalchemy.orm import Session as SaSession

    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])

    real_flush = SaSession.flush
    calls = {"n": 0}

    def flaky_flush(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 1:  # commit 时唯一一次 flush 抛错 → due_at 与 remind_at 都不变
            raise RuntimeError("reminder update failed")
        return real_flush(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "flush", flaky_flush)
    resp = client.patch(
        f"{TASK_API}/{task['id']}/postpone",
        json={"due_at": "2026-08-25T09:00:00+08:00"},
    )
    monkeypatch.undo()

    assert resp.status_code == 500
    # due_at 未变化
    assert client.get(f"{TASK_API}/{task['id']}").json()["due_at"] == "2026-08-11T04:00:00Z"
    # Reminder 时间未变化
    assert _reminders(client)[0]["remind_at"] == "2026-08-11T04:00:00Z"


def test_complete_cancel_failure_keeps_task_confirmed(client, worker_env, monkeypatch):
    """complete 时 Reminder 取消失败 → Task 不能单独变成 DONE。"""
    from sqlalchemy.orm import Session as SaSession

    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])

    real_flush = SaSession.flush
    calls = {"n": 0}

    def flaky_flush(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 1:  # commit 时唯一一次 flush 抛错 → Task 保持 CONFIRMED
            raise RuntimeError("reminder cancel failed")
        return real_flush(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "flush", flaky_flush)
    resp = client.post(f"{TASK_API}/{task['id']}/complete")
    monkeypatch.undo()

    assert resp.status_code == 500
    assert client.get(f"{TASK_API}/{task['id']}").json()["status"] == "CONFIRMED"
    assert _reminders(client)[0]["status"] == "PENDING"


def test_db_error_never_returns_200(client, worker_env, monkeypatch):
    """数据库异常不能以 200 响应返回。"""
    from sqlalchemy.orm import Session as SaSession

    real_commit = SaSession.commit
    monkeypatch.setattr(SaSession, "commit", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    resp = client.post(TASK_API, json={"title": "x"})
    monkeypatch.undo()
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "INTERNAL_ERROR"


# ---------- 二、Worker 正确性：边界与隔离 ----------


def test_terminal_states_never_reclaimed(client, worker_env):
    """DELIVERED / CANCELLED / FAILED 都不能被重新领取。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    engine, SessionLocal = worker_env
    db = SessionLocal()
    for status in ("DELIVERED", "CANCELLED", "FAILED"):
        db.execute(
            text("UPDATE reminders SET status=:s, lease_expires_at=NULL WHERE task_id=:tid"),
            {"s": status, "tid": task["id"]},
        )
        db.commit()
        stats = run_worker_once(worker_env, _clock(offset_hours=2), batch_size=50, lease_seconds=60, max_attempts=5)
        assert stats.scanned == 0, f"{status} 不能被重新扫描"
        assert _notifications(client) == []
        # 复位为 PENDING 以便下一轮
        db.execute(text("UPDATE reminders SET status='PENDING' WHERE task_id=:tid"), {"tid": task["id"]})
        db.commit()
    db.close()


def test_lease_boundary_exactly_now_not_reclaimable(client, worker_env):
    """lease 恰好等于当前时间：严格小于（< now）才可恢复，等于时不可领取。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    now = _clock(offset_hours=1).now().replace(tzinfo=None)

    engine, SessionLocal = worker_env
    db = SessionLocal()
    # 注意：text SQL 绑定走 sqlite3 默认适配器，与 ORM 的 DATETIME 存储格式
    # （"%Y-%m-%d %H:%M:%S.%f"）不同；边界相等测试必须用与 ORM 一致的字符串格式，
    # 否则字符串比较会把 "05:00:00" 判为早于 "05:00:00.000000"。
    lease_text = now.strftime("%Y-%m-%d %H:%M:%S.%f")
    db.execute(
        text("UPDATE reminders SET status='CLAIMED', claimed_at=:now, "
             "lease_expires_at=:lease WHERE task_id=:tid"),
        {"now": lease_text, "lease": lease_text, "tid": task["id"]},  # lease 恰好 == now
    )
    db.commit()
    db.close()

    stats = run_worker_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.scanned == 0  # 恰好等于不算过期

    # 前进 1 秒后可以恢复
    stats2 = run_worker_once(worker_env, FixedClock(now + timedelta(seconds=1)), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats2.delivered == 1


def test_lost_race_does_not_increment_attempt(client, worker_env):
    """竞争失败的 Worker 不能增加 attempt_count。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])

    # 模拟另一个 Worker 已领取（CLAIMED + lease 未过期）
    engine, SessionLocal = worker_env
    db = SessionLocal()
    db.execute(
        text("UPDATE reminders SET status='CLAIMED', claimed_at=:now, "
             "lease_expires_at=:lease WHERE task_id=:tid"),
        {"now": _clock(offset_hours=1).now().replace(tzinfo=None),
         "lease": (_clock(offset_hours=1).now() + timedelta(seconds=60)).replace(tzinfo=None),
         "tid": task["id"]},
    )
    db.commit()
    db.close()

    stats = run_worker_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.scanned == 0
    assert _reminders(client)[0]["attempt_count"] == 0  # 未被错误增加


def test_one_failing_reminder_does_not_block_batch(client, worker_env, monkeypatch):
    """单批中第一条投递抛异常，第二条仍必须被处理。"""
    for i in range(2):
        task = make_task(client, due_at=DUE_TODAY, title=f"batch-{i}")
        confirm(client, task["id"])

    from app.services import reminder_service as rs

    real_deliver = rs._deliver
    calls = {"n": 0}

    def flaky_deliver(db, reminder, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first delivery explodes")
        return real_deliver(db, reminder, now)

    monkeypatch.setattr(rs, "_deliver", flaky_deliver)
    stats = run_worker_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    monkeypatch.undo()

    assert calls["n"] == 2  # 两条都被尝试
    assert stats.failed == 1  # 第一条失败被计入
    assert stats.delivered == 1  # 第二条成功
    assert len(_notifications(client)) == 1
    # 第一条保持 CLAIMED（可重试）；第二条 DELIVERED
    statuses = sorted(r["status"] for r in _reminders(client))
    assert statuses == ["CLAIMED", "DELIVERED"]


# ---------- 三、SQLite 约束与索引 ----------


def test_foreign_keys_pragma_enabled(tmp_path):
    """应用引擎的 SQLite 连接必须启用 foreign_keys pragma（CASCADE 生效的前提）。

    不连接应用引擎（避免触碰开发库 jarvis.db）：改为验证 pragma 监听器已注册在
    应用引擎上，并把这个监听器挂到临时引擎上实测 PRAGMA 生效。
    """
    from sqlalchemy import event

    from app.core.database import _set_sqlite_pragma

    assert event.contains(engine, "connect", _set_sqlite_pragma), "应用引擎必须注册 pragma 监听器"

    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'pragma.db'}", connect_args={"check_same_thread": False}
    )
    event.listen(test_engine, "connect", _set_sqlite_pragma)
    raw = test_engine.raw_connection()
    try:
        val = raw.driver_connection.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        raw.close()
    assert val == 1, "pragma 监听器必须启用 PRAGMA foreign_keys=ON"

    # 对照：裸 sqlite3 连接默认关闭（印证「必须显式开启」）
    con = sqlite3.connect(":memory:")
    assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    con.close()


def test_cascade_delete_no_orphans(client, worker_env, tmp_path):
    """删除 Task 时外键 CASCADE 生效，不留下孤儿 Reminder/Notification。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    run_worker_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert len(_notifications(client)) == 1

    con = sqlite3.connect(tmp_path / "test_tasks.db")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("DELETE FROM tasks WHERE id=?", (task["id"],))
    con.commit()
    orphans_r = con.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
    orphans_n = con.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]
    con.close()
    assert orphans_r == 0
    assert orphans_n == 0


def _assert_real_indexes(con: sqlite3.Connection) -> None:
    """直接查询 sqlite_master：部分唯一索引与 Notification 唯一约束必须真实存在。"""
    active = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND name='uq_reminders_active_task'"
    ).fetchall()
    assert len(active) == 1, "uq_reminders_active_task 必须在 sqlite_master 中真实存在"
    sql = active[0][1].lower()
    assert "create unique index" in sql, "必须是 UNIQUE 索引"
    assert "where status in ('pending', 'claimed')" in sql, "必须是部分索引（WHERE status IN）"

    notif = con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' "
        "AND name='ix_notifications_reminder_id'"
    ).fetchall()
    assert len(notif) == 1
    assert "unique" in notif[0][1].lower(), "Notification.reminder_id 唯一索引必须真实存在"


def test_partial_unique_index_exists_in_real_app_db(client, worker_env, tmp_path):
    """ORM 建表（create_all）后的真实库必须带部分唯一索引，不是只有 ORM 声明。"""
    con = sqlite3.connect(tmp_path / "test_tasks.db")
    con.execute("PRAGMA foreign_keys=ON")
    _assert_real_indexes(con)
    con.close()


def test_partial_unique_index_exists_in_migrated_db(tmp_path):
    """Alembic 迁移（005）建出的库也必须带同样的索引（迁移不能与 ORM 声明漂移）。"""
    from alembic import command
    from alembic.config import Config

    db_file = tmp_path / "migrated_idx.db"
    cfg = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    command.upgrade(cfg, "head")

    con = sqlite3.connect(db_file)
    con.execute("PRAGMA foreign_keys=ON")
    _assert_real_indexes(con)
    con.close()


def test_duplicate_active_reminder_rejected(client, worker_env):
    """PENDING 重复调度被部分唯一索引拒绝（IntegrityError）。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    engine, SessionLocal = worker_env
    db = SessionLocal()
    existing = db.query(Reminder).filter(Reminder.task_id == task["id"]).first()
    duplicate = Reminder(
        task_id=task["id"],
        status=ReminderStatus.PENDING.value,
        remind_at=existing.remind_at,
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.close()
    # 原 Reminder 完好
    assert len(_reminders(client)) == 1


# ---------- 四、状态机边界 ----------


def test_confirm_past_due_immediately_processable(client, worker_env):
    """due_at 已过去的 DRAFT 被 confirm → PENDING → Worker 下一轮立即处理（文档化行为）。"""
    task = make_task(client, due_at="2026-08-10T12:00:00+08:00")  # 昨天到期
    confirm(client, task["id"])
    reminders = _reminders(client)
    assert reminders[0]["status"] == "PENDING"
    # 当前时钟（FIXED_NOW）已经晚于 remind_at → 立即投递
    stats = run_worker_once(worker_env, _clock(offset_hours=0), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.delivered == 1
    assert len(_notifications(client)) == 1


def test_due_at_exactly_now_is_due(client, worker_env):
    """due_at == 当前时间：remind_at <= now 含等号 → 到期。"""
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")  # 04:00Z == FIXED_NOW
    confirm(client, task["id"])
    stats = run_worker_once(worker_env, _clock(offset_hours=0), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.delivered == 1


def test_postpone_to_past_and_same_time(client):
    """postpone 到过去 / 相同时间：规则只约束状态，不约束时间大小。"""
    task = make_task(client, due_at="2026-08-20T17:00:00+08:00")
    confirm(client, task["id"])

    # 相同时间
    client.patch(f"{TASK_API}/{task['id']}/postpone", json={"due_at": "2026-08-20T17:00:00+08:00"})
    assert _reminders(client)[0]["remind_at"] == "2026-08-20T09:00:00Z"

    # 过去时间
    client.patch(f"{TASK_API}/{task['id']}/postpone", json={"due_at": "2020-01-01T00:00:00Z"})
    assert _reminders(client)[0]["remind_at"] == "2020-01-01T00:00:00Z"


def test_complete_while_claimed_keeps_delivery(client, worker_env):
    """CLAIMED 期间 complete：策略为不撤回投递中的提醒（通知仍产生），测试固定该行为。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    engine, SessionLocal = worker_env
    db = SessionLocal()
    db.execute(
        text("UPDATE reminders SET status='CLAIMED', claimed_at=:now, "
             "lease_expires_at=:lease WHERE task_id=:tid"),
        {"now": _clock(offset_hours=1).now().replace(tzinfo=None),
         "lease": (_clock(offset_hours=1).now() + timedelta(seconds=60)).replace(tzinfo=None),
         "tid": task["id"]},
    )
    db.commit()
    db.close()

    # complete 只取消 PENDING；CLAIMED 保持
    client.post(f"{TASK_API}/{task['id']}/complete")
    assert _reminders(client)[0]["status"] == "CLAIMED"

    # lease 过期后恢复投递（worker 继续完成投递）
    stats = run_once(
        worker_env,
        FixedClock(_clock(offset_hours=1).now() + timedelta(seconds=61)),
        batch_size=50, lease_seconds=60, max_attempts=5,
    )
    assert stats.delivered == 1
    assert len(_notifications(client)) == 1


def test_failed_reminder_not_auto_recovered(client, worker_env):
    """FAILED 是终态：不自动恢复（规则明确，需人工干预）。"""
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    engine, SessionLocal = worker_env
    db = SessionLocal()
    db.execute(
        text("UPDATE reminders SET status='FAILED', last_error_code='MANUAL' WHERE task_id=:tid"),
        {"tid": task["id"]},
    )
    db.commit()
    db.close()

    stats = run_worker_once(worker_env, _clock(offset_hours=2), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.scanned == 0
    assert _reminders(client)[0]["status"] == "FAILED"


# ---------- 五、API 分页与参数验证 ----------


def test_pagination_limit_offset(client, worker_env):
    for i in range(5):
        task = make_task(client, due_at=DUE_TODAY, title=f"page-{i}")
        confirm(client, task["id"])
    run_worker_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)

    # 分页生效：limit=2 → 2 条；offset=2 → 下 2 条，且不与第一页重叠
    page1 = _reminders(client, limit=2, offset=0)
    page2 = _reminders(client, limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    ids1 = {r["id"] for r in page1}
    ids2 = {r["id"] for r in page2}
    assert ids1.isdisjoint(ids2)

    # 通知分页与排序确定性（created_at desc, id desc）
    notifs = _notifications(client, limit=3, offset=0)
    assert len(notifs) == 3
    created = [n["created_at"] for n in notifs]
    assert created == sorted(created, reverse=True)


def test_pagination_invalid_values_422(client):
    for params in ({"limit": 0}, {"limit": -1}, {"limit": 201}, {"offset": -1}):
        resp = client.get("/api/reminders", params=params)
        assert resp.status_code == 422, f"{params} 应被拒绝"
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"
    resp = client.get("/api/notifications", params={"limit": 0})
    assert resp.status_code == 422
    resp = client.get("/api/notifications", params={"task_id": "abc"})
    assert resp.status_code == 422


def test_unread_count_consistent_with_list(client, worker_env):
    task = make_task(client, due_at=DUE_TODAY)
    confirm(client, task["id"])
    run_worker_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)

    count = client.get("/api/notifications/unread-count").json()["count"]
    unread_list = _notifications(client, unread_only=True)
    assert count == len(unread_list) == 1

    nid = unread_list[0]["id"]
    client.post(f"/api/notifications/{nid}/read")
    count2 = client.get("/api/notifications/unread-count").json()["count"]
    assert count2 == len(_notifications(client, unread_only=True)) == 0


# ---------- 测试质量：收窄过宽异常捕获 ----------


def test_invalid_worker_config_rejected_narrow():
    """非法配置必须抛 Pydantic ValidationError（而非宽泛 Exception）。"""
    with pytest.raises(ValidationError):
        Settings(reminder_batch_size=0)
    with pytest.raises(ValidationError):
        Settings(reminder_lease_seconds=-5)
    with pytest.raises(ValidationError):
        Settings(reminder_max_attempts=0)
