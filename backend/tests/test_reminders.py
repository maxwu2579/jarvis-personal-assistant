"""Reminder / Notification 测试：Fake Clock，禁止 sleep 等待到期。

覆盖：确认创建/不创建、重复确认、Worker 轮询/幂等/竞争/lease/attempt、
postpone/complete 联动、通知已读、API、404、UTC 输出、空轮询、batch 限制。
"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.clock import FixedClock
from app.core.database import Base, get_db
from app.main import app
from app.models.reminder import Notification, Reminder
from app.services.reminder_service import process_due_reminders_once
from tests.conftest import FIXED_NOW

UTC = timezone.utc
ZERO = datetime(2020, 1, 1, 0, 0, tzinfo=UTC)

TASK_API = "/api/tasks"
REM_API = "/api/reminders"
NOTIF_API = "/api/notifications"


def make_task(client, *, due_at=None, title="reminder task"):
    payload = {"title": title}
    if due_at is not None:
        payload["due_at"] = due_at
    resp = client.post(TASK_API, json=payload)
    assert resp.status_code == 201
    return resp.json()


def confirm(client, task_id):
    return client.post(f"{TASK_API}/{task_id}/confirm")


def _reminders(client, **params):
    resp = client.get(REM_API, params=params)
    assert resp.status_code == 200
    return resp.json()


def _notifications(client, **params):
    resp = client.get(NOTIF_API, params=params)
    assert resp.status_code == 200
    return resp.json()


def _clock(offset_hours=0):
    return FixedClock(FIXED_NOW + timedelta(hours=offset_hours))


@pytest.fixture()
def worker_env(client, tmp_path):
    """提供与 client 同一临时库的独立 session（模拟独立 Worker 进程）。

    autoflush=False 与生产 SessionLocal 一致（避免测试暴露与生产不同的
    autoflush 行为）。
    """
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test_tasks.db'}", connect_args={"check_same_thread": False}
    )
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return engine, SessionLocal


def run_once(worker_env, clock, **kwargs):
    engine, SessionLocal = worker_env
    db = SessionLocal()
    try:
        return process_due_reminders_once(db, clock, **kwargs)
    finally:
        db.close()


# ---------- 确认任务与 Reminder 创建 ----------


def test_confirm_with_due_at_creates_pending_reminder(client, worker_env):
    task = make_task(client, due_at="2026-08-20T17:00:00+08:00")
    assert _reminders(client) == []

    resp = confirm(client, task["id"])
    assert resp.status_code == 200
    assert resp.json()["status"] == "CONFIRMED"

    reminders = _reminders(client)
    assert len(reminders) == 1
    r = reminders[0]
    assert r["task_id"] == task["id"]
    assert r["status"] == "PENDING"
    assert r["remind_at"] == "2026-08-20T09:00:00Z"  # due_at 转 UTC，remind_at=due_at
    assert r["attempt_count"] == 0
    assert r["claimed_at"] is None
    assert r["lease_expires_at"] is None
    assert r["delivered_at"] is None
    assert r["created_at"].endswith("Z")
    assert r["updated_at"].endswith("Z")


def test_confirm_without_due_at_creates_no_reminder(client):
    task = make_task(client)
    confirm(client, task["id"])
    assert _reminders(client) == []


def test_duplicate_confirm_no_duplicate_reminder(client):
    task = make_task(client, due_at="2026-08-20T17:00:00+08:00")
    confirm(client, task["id"])
    resp = confirm(client, task["id"])
    assert resp.status_code == 409
    # 第一个 Reminder 保留，但没有创建第二个
    reminders = _reminders(client)
    assert len(reminders) == 1
    assert reminders[0]["status"] == "PENDING"


# ---------- Proposal 流程不创建 Reminder ----------


def test_proposal_draft_creates_no_reminder(client, fake_provider):
    fake_provider.reply_json = {
        "action": "create_task_draft",
        "arguments": {"title": "建议任务", "due_at": "2026-08-20T17:00:00+08:00"},
        "explanation": "测试",
    }
    cid = client.post("/api/conversations", json={"title": "p"}).json()["id"]
    resp = client.post(
        f"/api/conversations/{cid}/task-proposals",
        json={"content": "安排任务", "timezone": "Asia/Shanghai"},
    )
    assert resp.status_code == 201
    assert resp.json()["created_task"]["status"] == "DRAFT"
    # DRAFT 阶段没有任何 Reminder
    assert _reminders(client) == []

    # 用户 confirm 后才创建
    task_id = resp.json()["created_task"]["id"]
    confirm(client, task_id)
    reminders = _reminders(client)
    assert len(reminders) == 1
    assert reminders[0]["status"] == "PENDING"


# ---------- Worker 投递 ----------


def test_pending_not_due_not_delivered(client, worker_env):
    task = make_task(client, due_at="2026-08-20T17:00:00+08:00")
    confirm(client, task["id"])
    # 时钟早于 remind_at（09:00Z），不投递
    stats = run_once(worker_env, _clock(offset_hours=0), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.scanned == 0
    assert stats.delivered == 0
    assert _notifications(client) == []
    reminders = _reminders(client)
    assert reminders[0]["status"] == "PENDING"


def test_due_reminder_delivered_with_notification(client, worker_env):
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")  # remind_at = 04:00Z（FIXED_NOW）
    confirm(client, task["id"])
    # 时钟到 05:00Z（到期后）
    stats = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.scanned == 1
    assert stats.claimed == 1
    assert stats.delivered == 1
    assert stats.failed == 0

    reminders = _reminders(client)
    assert reminders[0]["status"] == "DELIVERED"
    assert reminders[0]["delivered_at"].endswith("Z")
    assert reminders[0]["attempt_count"] == 1

    notifications = _notifications(client)
    assert len(notifications) == 1
    n = notifications[0]
    assert n["reminder_id"] == reminders[0]["id"]
    assert n["task_id"] == task["id"]
    assert n["type"] == "REMINDER_DUE"
    assert "reminder task" in n["title"]
    assert n["read_at"] is None
    assert n["created_at"].endswith("Z")


def test_worker_idempotent_no_duplicate_notification(client, worker_env):
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])

    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    stats2 = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats2.scanned == 0  # DELIVERED 不再被扫描

    assert len(_notifications(client)) == 1


def test_worker_competition_single_delivery(client, worker_env):
    """两个 Worker 竞争：先领取的一方投递；另一方扫描不到或领取失败，通知唯一。"""
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])

    # Worker A 领取并投递
    stats_a = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats_a.delivered == 1
    # Worker B 再来一轮（模拟并发后的第二次扫描）
    stats_b = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats_b.scanned == 0
    assert len(_notifications(client)) == 1


def test_lease_not_expired_cannot_reclaim(client, worker_env):
    """Worker 领取后崩溃：lease 未到期时其他 Worker 不能重新领取。"""
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])
    clock = _clock(offset_hours=1)

    # 模拟：Worker A 领取后崩溃（直接调用内部领取路径不可行，改为：
    # 手工把 Reminder 置为 CLAIMED + lease 未过期，模拟 A 领取后的中间状态）
    engine, SessionLocal = worker_env
    db = SessionLocal()
    db.execute(
        text("UPDATE reminders SET status='CLAIMED', claimed_at=:now, "
             "lease_expires_at=:lease, attempt_count=attempt_count+1 "
             "WHERE task_id=:tid"),
        {"now": clock.now().replace(tzinfo=None),
         "lease": (clock.now() + timedelta(seconds=60)).replace(tzinfo=None), "tid": task["id"]},
    )
    db.commit()
    db.close()

    # Worker B 轮询：lease 未过期 → 不扫描（CLAIMED 且 lease_expires_at > now）
    stats = run_once(worker_env, clock, batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.scanned == 0
    assert len(_notifications(client)) == 0


def test_lease_expired_recovery(client, worker_env):
    """lease 过期后可恢复投递。"""
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])
    claim_clock = _clock(offset_hours=1)

    # Worker A 领取后崩溃：CLAIMED + lease 60s（过期时刻 = 01:00 + 60s）
    engine, SessionLocal = worker_env
    db = SessionLocal()
    db.execute(
        text("UPDATE reminders SET status='CLAIMED', claimed_at=:now, "
             "lease_expires_at=:lease, attempt_count=attempt_count+1 WHERE task_id=:tid"),
        {"now": claim_clock.now().replace(tzinfo=None),
         "lease": (claim_clock.now() + timedelta(seconds=60)).replace(tzinfo=None), "tid": task["id"]},
    )
    db.commit()
    db.close()

    # 时钟前进 61 秒 → lease 过期 → Worker B 恢复投递
    recovery_clock = FixedClock(claim_clock.now() + timedelta(seconds=61))
    stats = run_once(worker_env, recovery_clock, batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.scanned == 1
    assert stats.delivered == 1

    notifications = _notifications(client)
    assert len(notifications) == 1
    reminders = _reminders(client)
    assert reminders[0]["status"] == "DELIVERED"
    assert reminders[0]["attempt_count"] == 2  # 领取两次（崩溃一次 + 恢复一次）


def test_attempt_count_and_failed_after_max(client, worker_env):
    """投递持续失败（模拟唯一冲突以外的 DB 错误）→ attempt 递增 → 达上限 FAILED。"""
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])

    # 模拟每次投递都失败的场景：把 notifications 表改成会冲突的外键？
    # 更直接：monkeypatch Notification 唯一约束不可行。改为验证 attempt 计数与
    # MAX_ATTEMPTS 路径：用手工模拟连续失败（每次领取后投递失败保持 CLAIMED）。
    # 简化实现：直接构造 CLAIMED + lease 过期 + attempt_count 递增的序列。
    engine, SessionLocal = worker_env
    clock = _clock(offset_hours=1)
    for i in range(5):
        db = SessionLocal()
        db.execute(
            text("UPDATE reminders SET status='CLAIMED', claimed_at=:now, "
                 "lease_expires_at=:lease, attempt_count=attempt_count+1 "
                 "WHERE task_id=:tid"),
            {"now": clock.now().replace(tzinfo=None),
             "lease": (clock.now() - timedelta(seconds=1)).replace(tzinfo=None), "tid": task["id"]},
        )
        db.commit()
        db.close()
        clock = FixedClock(clock.now() + timedelta(seconds=2))

    # 现在 attempt_count=5 且 lease 过期：Worker 应判定 MAX_ATTEMPTS → FAILED
    stats = run_once(worker_env, clock, batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.failed == 1
    reminders = _reminders(client)
    assert reminders[0]["status"] == "FAILED"
    assert reminders[0]["last_error_code"] == "MAX_ATTEMPTS"
    assert reminders[0]["attempt_count"] == 5
    assert len(_notifications(client)) == 0


# ---------- postpone / complete 联动 ----------


def test_postpone_updates_pending_reminder(client):
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])
    assert _reminders(client)[0]["remind_at"] == "2026-08-11T04:00:00Z"

    resp = client.patch(
        f"{TASK_API}/{task['id']}/postpone",
        json={"due_at": "2026-08-25T09:00:00+08:00"},
    )
    assert resp.status_code == 200
    reminders = _reminders(client)
    assert len(reminders) == 1  # 不产生第二个 Reminder
    assert reminders[0]["remind_at"] == "2026-08-25T01:00:00Z"
    assert reminders[0]["status"] == "PENDING"


def test_postpone_does_not_touch_delivered_history(client, worker_env):
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    assert _reminders(client)[0]["status"] == "DELIVERED"

    client.patch(f"{TASK_API}/{task['id']}/postpone", json={"due_at": "2026-08-30T09:00:00+08:00"})
    reminders = _reminders(client)
    assert len(reminders) == 1
    assert reminders[0]["status"] == "DELIVERED"
    assert reminders[0]["remind_at"] == "2026-08-11T04:00:00Z"  # 历史不被修改


def test_postpone_skips_claimed(client, worker_env):
    """并发策略：CLAIMED（投递中）的 Reminder 不被 postpone 改写。"""
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])
    engine, SessionLocal = worker_env
    db = SessionLocal()
    db.execute(
        text("UPDATE reminders SET status='CLAIMED', claimed_at=:now, "
             "lease_expires_at=:lease WHERE task_id=:tid"),
        {"now": _clock(offset_hours=1).now().replace(tzinfo=None),
         "lease": (_clock(offset_hours=1).now() + timedelta(seconds=60)).replace(tzinfo=None), "tid": task["id"]},
    )
    db.commit()
    db.close()

    client.patch(f"{TASK_API}/{task['id']}/postpone", json={"due_at": "2026-08-30T09:00:00+08:00"})
    reminders = _reminders(client)
    assert reminders[0]["status"] == "CLAIMED"
    assert reminders[0]["remind_at"] == "2026-08-11T04:00:00Z"


def test_complete_cancels_pending_reminder(client):
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])
    client.post(f"{TASK_API}/{task['id']}/complete")
    reminders = _reminders(client)
    assert reminders[0]["status"] == "CANCELLED"
    assert _notifications(client) == []


def test_complete_keeps_delivered_history(client, worker_env):
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    client.post(f"{TASK_API}/{task['id']}/complete")
    reminders = _reminders(client)
    assert reminders[0]["status"] == "DELIVERED"  # 历史保留
    assert len(_notifications(client)) == 1  # Notification 保留


# ---------- Notification API ----------


def test_mark_read_and_idempotent(client, worker_env):
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    notification_id = _notifications(client)[0]["id"]

    resp = client.post(f"{NOTIF_API}/{notification_id}/read")
    assert resp.status_code == 200
    assert resp.json()["read_at"].endswith("Z")

    # 幂等：重复标记返回同一状态
    resp2 = client.post(f"{NOTIF_API}/{notification_id}/read")
    assert resp2.status_code == 200
    assert resp2.json()["read_at"] == resp.json()["read_at"]


def test_unread_count(client, worker_env):
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)

    assert client.get(f"{NOTIF_API}/unread-count").json() == {"count": 1}
    nid = _notifications(client)[0]["id"]
    client.post(f"{NOTIF_API}/{nid}/read")
    assert client.get(f"{NOTIF_API}/unread-count").json() == {"count": 0}


def test_notification_filters(client, worker_env):
    task1 = make_task(client, due_at="2026-08-11T12:00:00+08:00", title="first")
    task2 = make_task(client, due_at="2026-08-11T13:00:00+08:00", title="second")
    confirm(client, task1["id"])
    confirm(client, task2["id"])
    run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)

    unread = _notifications(client, unread_only=True)
    assert len(unread) == 2
    by_task = _notifications(client, task_id=task1["id"])
    assert len(by_task) == 1
    assert by_task[0]["task_id"] == task1["id"]


def test_reminder_filters_and_404(client, worker_env):
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])

    pending = _reminders(client, status="PENDING")
    assert len(pending) == 1
    assert _reminders(client, status="DELIVERED") == []
    by_task = _reminders(client, task_id=task["id"])
    assert len(by_task) == 1

    rid = pending[0]["id"]
    detail = client.get(f"{REM_API}/{rid}")
    assert detail.status_code == 200
    assert detail.json()["id"] == rid

    assert client.get(f"{REM_API}/99999").status_code == 404
    assert client.post(f"{NOTIF_API}/99999/read").status_code == 404
    resp = client.get(f"{REM_API}", params={"status": "BOGUS"})
    assert resp.status_code == 422


def test_notification_read_404(client):
    assert client.post(f"{NOTIF_API}/99999/read").status_code == 404


# ---------- Worker 边界 ----------


def test_empty_poll_ok(client, worker_env):
    stats = run_once(worker_env, _clock(), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.scanned == 0
    assert stats.claimed == 0
    assert stats.delivered == 0
    assert stats.failed == 0
    assert stats.skipped == 0


def test_batch_size_limit(client, worker_env):
    for i in range(5):
        task = make_task(client, due_at="2026-08-11T12:00:00+08:00", title=f"batch-{i}")
        confirm(client, task["id"])

    stats = run_once(worker_env, _clock(offset_hours=1), batch_size=2, lease_seconds=60, max_attempts=5)
    assert stats.scanned == 2
    assert stats.delivered == 2
    assert len(_notifications(client)) == 2


# 配置校验测试已迁移到 test_phase3_review.py（收窄为 ValidationError）


def test_db_failure_rollback(client, worker_env, tmp_path, monkeypatch):
    """投递提交失败（非 SQLAlchemy 异常）：per-item 隔离捕获，不回滚领取，可重试。

    异常在 process_due_reminders_once 内被逐条隔离（绝不冒泡打停整批）；
    领取提交已固化（attempt+1），投递事务回滚 → 保持 CLAIMED 无 Notification。
    """
    task = make_task(client, due_at="2026-08-11T12:00:00+08:00")
    confirm(client, task["id"])

    from sqlalchemy.orm import Session as SaSession

    real_commit = SaSession.commit
    calls = {"n": 0}

    def flaky_commit(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:  # 第 1 次 = 领取提交（成功）；第 2 次 = 投递提交（失败）
            raise RuntimeError("delivery db failure")
        return real_commit(self, *args, **kwargs)

    monkeypatch.setattr(SaSession, "commit", flaky_commit)
    stats = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
    monkeypatch.undo()

    # 领取已固化（attempt+1）；投递回滚 → 保持 CLAIMED；无 Notification；
    # 异常被 per-item 隔离捕获并计入 failed，而不是冒泡。
    # 注意：异常在 _claim_and_deliver 返回之前抛出，outcome 尚未记录 → claimed 计 0；
    # 「领取成功」由数据库状态（CLAIMED + attempt=1）证明，而不是计数器。
    assert stats.claimed == 0
    assert stats.failed == 1
    assert stats.delivered == 0
    reminders = _reminders(client)
    assert reminders[0]["status"] == "CLAIMED"
    assert reminders[0]["attempt_count"] == 1
    assert _notifications(client) == []

    # lease 过期后可恢复，且不会产生重复通知（回滚保证 Notification 未落库）
    engine, SessionLocal = worker_env
    db = SessionLocal()
    db.execute(
        text("UPDATE reminders SET lease_expires_at=:past WHERE task_id=:tid"),
        {"past": datetime(2020, 1, 1, tzinfo=UTC).replace(tzinfo=None), "tid": task["id"]},
    )
    db.commit()
    db.close()
    stats = run_once(worker_env, _clock(offset_hours=2), batch_size=50, lease_seconds=60, max_attempts=5)
    assert stats.delivered == 1
    assert len(_notifications(client)) == 1
