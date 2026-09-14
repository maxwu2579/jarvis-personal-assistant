"""Reminder 领域逻辑与 Worker 单次处理。

并发与幂等设计（SQLite 可实现的安全方案，不夸大并发能力）：
- 原子领取：单条条件 UPDATE（status 条件 + lease 条件），SQLite 单写者串行化
  保证两个 Worker 只有一个能领取成功（rowcount=0 的一方跳过）；
- lease：领取后设置 lease_expires_at，Worker 崩溃后到期即可被重新领取；
- 幂等投递：Notification.reminder_id 唯一约束是最终防重保障——重复投递
  触发唯一冲突时按「已投递」处理；
- 数据库是唯一事实来源；不使用内存 Timer；
- 不承诺 exactly-once，实现并描述「幂等投递」（at-least-once + 去重）。

任务侧副作用（与 Task 状态机同一事务）：
- schedule_on_confirm：确认时若有 due_at 则创建 PENDING Reminder（remind_at=due_at）；
- reschedule_on_postpone：只更新 PENDING 的 remind_at；CLAIMED（正在投递）与
  DELIVERED 历史不修改（并发策略：投递中的提醒不再改写，文档说明）；
- cancel_on_complete：取消 PENDING；DELIVERED 历史保留。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.database import DIALECT
from app.core.dialect import coerce_utc_for_db
from app.models.reminder import (
    Notification,
    NotificationType,
    Reminder,
    ReminderStatus,
)
from app.models.task import Task

logger = logging.getLogger(__name__)

PENDING = ReminderStatus.PENDING.value
CLAIMED = ReminderStatus.CLAIMED.value
DELIVERED = ReminderStatus.DELIVERED.value
CANCELLED = ReminderStatus.CANCELLED.value
FAILED = ReminderStatus.FAILED.value


def _to_db_utc(value: datetime) -> datetime:
    """写入数据库前的 UTC 归一化（方言接缝，见 dialect.coerce_utc_for_db）。

    - SQLite：既有约定存 naive UTC（Phase 0 起统一；SQLAlchemy 对 SQLite
      的 DateTime 比较做 Python 侧求值，aware 与读回 naive 混比抛 TypeError）；
    - PostgreSQL：TIMESTAMPTZ 列必须与 aware UTC 比较，否则 naive 参数会被
      按会话时区解释（偏移错误）。
    """
    return coerce_utc_for_db(value, DIALECT)


@dataclass(slots=True)
class WorkerStats:
    """一次 Worker 轮询的统计。"""

    scanned: int = 0
    claimed: int = 0
    delivered: int = 0
    failed: int = 0
    skipped: int = 0


# ---------- Task 事务集成（不 commit，由调用方统一提交） ----------


def create_for_action_confirmation(
    db: Session, *, task: Task, remind_at: datetime
) -> Reminder:
    """Create one pending Reminder for a newly flushed Task without committing.

    Phase 8B owns the surrounding Task + Reminder + idempotency-receipt
    transaction.  This helper deliberately performs no commit and never derives
    ``remind_at`` from ``task.due_at`` because those fields have distinct meaning.
    """
    reminder = Reminder(
        task_id=task.id,
        status=PENDING,
        remind_at=_to_db_utc(remind_at),
    )
    db.add(reminder)
    return reminder


def schedule_on_confirm(db: Session, task: Task) -> Reminder | None:
    """确认任务时安排提醒：有 due_at 才创建 PENDING Reminder（remind_at=due_at）。

    同一事务内调用；活动提醒部分唯一索引防止重复创建。
    """
    if task.due_at is None:
        return None
    reminder = Reminder(task_id=task.id, status=PENDING, remind_at=_to_db_utc(task.due_at))
    db.add(reminder)
    return reminder


def reschedule_on_postpone(db: Session, task: Task) -> Reminder | None:
    """延期任务时同步 PENDING Reminder 的 remind_at。

    并发策略：只更新 PENDING；CLAIMED（正在投递）与 DELIVERED 历史不修改——
    投递中的提醒已不可撤回，改时间已无意义；该策略由测试固定。
    """
    reminder = (
        db.query(Reminder)
        .filter(Reminder.task_id == task.id, Reminder.status == PENDING)
        .first()
    )
    if reminder is not None:
        reminder.remind_at = _to_db_utc(task.due_at)
    return reminder


def cancel_on_complete(db: Session, task: Task) -> int:
    """完成任务时取消未投递的 PENDING Reminder；DELIVERED 历史保留。"""
    result = (
        db.query(Reminder)
        .filter(Reminder.task_id == task.id, Reminder.status == PENDING)
        .update({Reminder.status: CANCELLED})
    )
    return result


# ---------- Worker 单次处理 ----------


def process_due_reminders_once(
    db: Session,
    clock: Clock,
    *,
    batch_size: int,
    lease_seconds: int,
    max_attempts: int,
) -> WorkerStats:
    """单次轮询：领取到期提醒 → 投递 Notification → 标记 DELIVERED。

    可安全重复调用；可被多个 Worker 并发调用（条件 UPDATE + 唯一约束）。
    """
    now = _to_db_utc(clock.now())
    stats = WorkerStats()

    candidates = (
        db.query(Reminder)
        .filter(
            or_(
                and_(Reminder.status == PENDING, Reminder.remind_at <= now),
                and_(Reminder.status == CLAIMED, Reminder.lease_expires_at < now),
            )
        )
        # 确定性排序：remind_at 相同（如同一 due_at 批量 confirm）时按 id 升序，
        # 保证同一批内处理顺序可复现（测试依赖该顺序做故障注入）。
        .order_by(Reminder.remind_at.asc(), Reminder.id.asc())
        .limit(batch_size)
        .all()
    )
    stats.scanned = len(candidates)

    for reminder in candidates:
        if reminder.attempt_count >= max_attempts:
            _mark_failed(db, reminder, "MAX_ATTEMPTS")
            stats.failed += 1
            continue

        # 每条 Reminder 独立处理且异常隔离：一条失败绝不能阻塞同一批后续记录
        # （毒消息由 MAX_ATTEMPTS 兜底，而不是让整批停摆）。
        try:
            outcome = _claim_and_deliver(db, reminder, now, lease_seconds)
        except Exception as exc:  # noqa: BLE001 - Worker 循环必须存活
            db.rollback()
            logger.warning(
                "reminder.item_error id=%s type=%s",
                reminder.id,
                type(exc).__name__,
            )
            stats.failed += 1
            continue

        if outcome == "delivered":
            stats.claimed += 1
            stats.delivered += 1
        elif outcome == "claimed":
            stats.claimed += 1  # 领取成功但投递暂不可行（可重试，等 lease）
        elif outcome == "failed":
            stats.failed += 1
        else:  # skipped
            stats.skipped += 1

    logger.info(
        "reminder.worker_once",
        extra={
            "scanned": stats.scanned,
            "claimed": stats.claimed,
            "delivered": stats.delivered,
            "failed": stats.failed,
            "skipped": stats.skipped,
            "now": now.isoformat(),
        },
    )
    return stats


def _claim_and_deliver(db: Session, reminder: Reminder, now: datetime, lease_seconds: int) -> str:
    """原子领取 → 投递。返回 delivered / claimed / failed / skipped。"""
    new_attempt = reminder.attempt_count + 1
    lease_expires = _to_db_utc(now + timedelta(seconds=lease_seconds))

    # 原子领取：条件 UPDATE。SQLite 单写者，两个 Worker 竞争时只有一个 rowcount=1。
    result = db.execute(
        update(Reminder)
        .where(
            Reminder.id == reminder.id,
            or_(
                and_(Reminder.status == PENDING, Reminder.remind_at <= now),
                and_(Reminder.status == CLAIMED, Reminder.lease_expires_at < now),
            ),
        )
        .values(
            status=CLAIMED,
            claimed_at=now,
            lease_expires_at=lease_expires,
            attempt_count=new_attempt,
        )
    )
    db.commit()  # 领取固化（领取即计数：失败也会增加 attempt_count）
    if result.rowcount == 0:
        return "skipped"  # 被其他 Worker 领取或状态已变化

    db.refresh(reminder)
    return _deliver(db, reminder, now)


def _deliver(db: Session, reminder: Reminder, now: datetime) -> str:
    """创建 Notification 并标记 DELIVERED（同一事务）。

    - 唯一约束冲突（重复投递）→ 视为已投递（幂等）；
    - 其他数据错误（如外键）→ 永久 FAILED；
    - 其他数据库错误 → 可重试（保持 CLAIMED，等 lease 过期）。
    """
    task = db.get(Task, reminder.task_id)
    title = task.title if task else "任务提醒"
    try:
        notification = Notification(
            reminder_id=reminder.id,
            task_id=reminder.task_id,
            type=NotificationType.REMINDER_DUE.value,
            title=f"任务提醒：{title}",
            body=f"任务「{title}」已到截止时间。",
        )
        db.add(notification)
        reminder.status = DELIVERED
        reminder.delivered_at = now
        reminder.last_error_code = None
        db.commit()
        return "delivered"
    except IntegrityError as exc:
        db.rollback()
        # 唯一冲突：reminder_id 已有 Notification → 幂等，视为已投递
        existing = (
            db.query(Notification).filter(Notification.reminder_id == reminder.id).first()
        )
        if existing is not None:
            db.execute(
                update(Reminder)
                .where(Reminder.id == reminder.id)
                .values(status=DELIVERED, delivered_at=now)
            )
            db.commit()
            return "delivered"
        # 其他唯一/外键冲突：永久数据错误
        _mark_failed(db, reminder, "DELIVERY_UNIQUE_CONFLICT")
        return "failed"
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("reminder.delivery_retryable id=%s error=%s", reminder.id, type(exc).__name__)
        return "claimed"  # 保持 CLAIMED，lease 过期后重试


def _mark_failed(db: Session, reminder: Reminder, error_code: str) -> None:
    db.execute(
        update(Reminder)
        .where(Reminder.id == reminder.id)
        .values(status=FAILED, last_error_code=error_code)
    )
    db.commit()
    logger.warning("reminder.failed id=%s code=%s attempts=%s", reminder.id, error_code, reminder.attempt_count)
