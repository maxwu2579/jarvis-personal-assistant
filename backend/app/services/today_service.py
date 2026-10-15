"""Phase 11A Today：只读的「本地日」聚合投影。

纯投影：只读取 Task / Reminder / Notification / Execution 当前的事实来源，
不写入、不 flush、不 commit，不持有任何 Today 状态，也不缓存。
本模块不调用 LLM、Calendar / Graph、Memory、Chat、RAG 或任何执行工具。

日界（用户浏览器的 IANA 时区）：
    [本地日期 00:00, 下一本地日期 00:00)
两个边界分别按「日历日期 + 时区」构造后再转换为 UTC；绝不使用「起点 + 24 小时」。
本地午夜不存在（被时区跳过）时，边界是该本地日期的第一个有效时刻；每个边界都要
通过往返校验证明它确实是该日期的第一个时刻，否则 fail closed（内部错误，不猜测）。

四个 section 相互独立：某个 section 的数据库错误只把该 section 标记为 unavailable，
其余照常返回；非数据库异常不被吞掉。
"""

import logging
import zoneinfo
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from typing import Callable

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, aliased

from app.core.database import DIALECT
from app.core.dialect import coerce_utc_for_db
from app.models.execution import TERMINAL_STATUSES, Execution, ExecutionStatus
from app.models.execution_step import ConfirmationStatus, ExecutionStep
from app.models.reminder import Notification, Reminder, ReminderStatus
from app.models.task import Task, TaskStatus
from app.schemas.today import (
    ExecutionList,
    ExecutionSection,
    NotificationList,
    NotificationSection,
    OtherOpenTasks,
    ReminderList,
    ReminderSection,
    TaskList,
    TaskSection,
    TodayExecution,
    TodayNotification,
    TodayReminder,
    TodayResponse,
    TodayTask,
)
from app.services.execution_registry import PUBLIC_EXECUTION_TYPES
from app.services.execution_service import DEFAULT_OWNER_ID

logger = logging.getLogger(__name__)

TIMEZONE_MAX_LENGTH = 64
TASK_LIMIT = 50
REMINDER_LIMIT = 50
NOTIFICATION_LIMIT = 20
EXECUTION_LIMIT = 20
LOOKBACK_DAYS = 6  # 连同今天共 7 个本地日期
_ONE_MICROSECOND = timedelta(microseconds=1)


class TodayBoundaryError(RuntimeError):
    """日界无法被证明为本地日期的第一个时刻：fail closed（→ 既有通用 500）。

    消息固定，不含时区名或日期。
    """

    def __init__(self) -> None:
        super().__init__("Today day boundary could not be proven")


class TodayUnavailableError(Exception):
    """四个本地 section 全部不可读（→ 503 SERVICE_UNAVAILABLE）。"""


@dataclass(frozen=True)
class DayWindow:
    """一个本地日的 UTC 时刻边界（aware UTC）。"""

    local_date: date
    day_start: datetime
    day_end: datetime
    lookback_start: datetime


@lru_cache(maxsize=1)
def _known_timezones() -> frozenset[str]:
    return frozenset(zoneinfo.available_timezones())


def resolve_timezone(name: object) -> zoneinfo.ZoneInfo | None:
    """已安装 tzdata 中的 IANA 时区 → ZoneInfo；其余一律 None（无任何回退）。"""
    if not isinstance(name, str) or not 1 <= len(name) <= TIMEZONE_MAX_LENGTH:
        return None
    if name not in _known_timezones():
        return None
    try:
        return zoneinfo.ZoneInfo(name)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError, OSError):
        return None


def first_instant_of_local_date(local_date: date, zone: zoneinfo.ZoneInfo) -> datetime:
    """本地日期的第一个有效时刻（aware UTC）。

    候选 = fold=0 的本地 00:00 → UTC。无论 00:00 是否真实存在，都必须证明：
    候选往返回本地后日期等于目标日期，且它前 1 微秒的时刻属于更早的本地日期。
    证明失败即 fail closed，绝不猜测、不回退到服务器时区、不使用固定时长。
    """
    candidate = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc)
    if candidate.astimezone(zone).date() != local_date:
        raise TodayBoundaryError()
    if (candidate - _ONE_MICROSECOND).astimezone(zone).date() >= local_date:
        raise TodayBoundaryError()
    return candidate


def local_midnight_exists(local_date: date, zone: zoneinfo.ZoneInfo) -> bool:
    """本地 00:00 在该日期是否真实存在（往返后仍是 00:00）。"""
    back = datetime.combine(local_date, time.min, tzinfo=zone).astimezone(timezone.utc).astimezone(zone)
    return back.date() == local_date and back.time() == time.min


def compute_day_window(now: datetime, zone: zoneinfo.ZoneInfo) -> DayWindow:
    """now（aware）所在本地日的边界。「+1 天 / −6 天」是对日期值的日历运算，不是对时刻加时长。"""
    if now.tzinfo is None:
        raise TodayBoundaryError()
    local_date = now.astimezone(zone).date()
    return DayWindow(
        local_date=local_date,
        day_start=first_instant_of_local_date(local_date, zone),
        day_end=first_instant_of_local_date(local_date + timedelta(days=1), zone),
        lookback_start=first_instant_of_local_date(local_date - timedelta(days=LOOKBACK_DAYS), zone),
    )


def _db(value: datetime) -> datetime:
    """唯一的存储比较接缝：aware UTC → 各方言的既有存储表示。"""
    return coerce_utc_for_db(value, DIALECT)


def _count(db: Session, model, *conditions) -> int:
    return db.execute(select(func.count()).select_from(model).where(*conditions)).scalar_one()


# ---------------------------------------------------------------- tasks

def _task_list(db: Session, conditions: tuple, order: tuple) -> TaskList:
    total = _count(db, Task, *conditions)
    rows = db.execute(select(Task).where(*conditions).order_by(*order).limit(TASK_LIMIT)).scalars().all()
    return TaskList(
        items=[TodayTask(id=t.id, title=t.title, status=t.status, due_at=t.due_at) for t in rows],
        total=total,
        truncated=total > TASK_LIMIT,
    )


def _tasks(db: Session, window: DayWindow) -> TaskSection:
    start, end = _db(window.day_start), _db(window.day_end)
    draft = Task.status == TaskStatus.DRAFT.value
    confirmed = Task.status == TaskStatus.CONFIRMED.value
    by_due = (Task.due_at.asc(), Task.id.asc())
    return TaskSection(
        status="ok",
        drafts=_task_list(db, (draft,), (Task.due_at.is_(None).asc(), *by_due)),  # nulls last
        overdue=_task_list(db, (confirmed, Task.due_at.is_not(None), Task.due_at < start), by_due),
        due_today=_task_list(db, (confirmed, Task.due_at >= start, Task.due_at < end), by_due),
        other_open=OtherOpenTasks(
            later=_count(db, Task, confirmed, Task.due_at >= end),
            no_due_date=_count(db, Task, confirmed, Task.due_at.is_(None)),
        ),
    )


# ---------------------------------------------------------------- reminders

def _reminder_list(db: Session, condition) -> ReminderList:
    total = _count(db, Reminder, condition)
    rows = db.execute(
        select(Reminder, Task.title)
        .join(Task, Task.id == Reminder.task_id)
        .where(condition)
        .order_by(Reminder.remind_at.asc(), Reminder.id.asc())
        .limit(REMINDER_LIMIT)
    ).all()
    return ReminderList(
        items=[
            TodayReminder(id=r.id, task_id=r.task_id, task_title=title, status=r.status, remind_at=r.remind_at)
            for r, title in rows
        ],
        total=total,
        truncated=total > REMINDER_LIMIT,
    )


def _reminders(db: Session, window: DayWindow) -> ReminderSection:
    start, end, lookback = _db(window.day_start), _db(window.day_end), _db(window.lookback_start)
    undelivered = Reminder.status.in_((ReminderStatus.PENDING.value, ReminderStatus.CLAIMED.value))
    today = and_(Reminder.remind_at >= start, Reminder.remind_at < end)
    return ReminderSection(
        status="ok",
        scheduled=_reminder_list(db, and_(undelivered, today)),
        delivered=_reminder_list(db, and_(Reminder.status == ReminderStatus.DELIVERED.value, today)),
        attention=_reminder_list(db, or_(
            and_(undelivered, Reminder.remind_at < start),
            and_(Reminder.status == ReminderStatus.FAILED.value,
                 Reminder.remind_at >= lookback, Reminder.remind_at < end),
        )),
    )


# ---------------------------------------------------------------- notifications

def _notifications(db: Session, _window: DayWindow) -> NotificationSection:
    unread = Notification.read_at.is_(None)
    total = _count(db, Notification, unread)
    rows = db.execute(
        select(Notification).where(unread)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .limit(NOTIFICATION_LIMIT)
    ).scalars().all()
    return NotificationSection(
        status="ok",
        unread=NotificationList(
            items=[TodayNotification(id=n.id, task_id=n.task_id, title=n.title, body=n.body,
                                     created_at=n.created_at) for n in rows],
            total=total,
            truncated=total > NOTIFICATION_LIMIT,
        ),
    )


# ---------------------------------------------------------------- executions

def _executions(db: Session, window: DayWindow) -> ExecutionSection:
    end, lookback = _db(window.day_end), _db(window.lookback_start)
    occurred = func.coalesce(Execution.finished_at, Execution.updated_at)
    public = and_(
        Execution.owner_id == DEFAULT_OWNER_ID,
        Execution.execution_type.in_(tuple(PUBLIC_EXECUTION_TYPES)),
    )
    awaiting_confirmation = exists(
        select(ExecutionStep.id).where(
            ExecutionStep.execution_id == Execution.id,
            ExecutionStep.requires_confirmation.is_(True),
            ExecutionStep.confirmation_status.in_(
                (ConfirmationStatus.PENDING.value, ConfirmationStatus.EXPIRED.value)
            ),
        )
    )
    retry = aliased(Execution)
    superseded = exists(select(retry.id).where(retry.retry_of_id == Execution.id))
    recent = and_(occurred >= lookback, occurred < end)
    reasons = (
        ("confirmation_required", and_(
            public, Execution.status.not_in(tuple(s.value for s in TERMINAL_STATUSES)), awaiting_confirmation)),
        ("failed", and_(public, Execution.status == ExecutionStatus.FAILED.value, recent, ~superseded)),
        ("timed_out", and_(public, Execution.status == ExecutionStatus.TIMED_OUT.value, recent, ~superseded)),
    )
    items: list[TodayExecution] = []
    total = 0
    for reason, condition in reasons:  # 固定顺序：待确认 → 失败 → 超时
        total += _count(db, Execution, condition)
        remaining = EXECUTION_LIMIT - len(items)
        if remaining <= 0:
            continue
        rows = db.execute(
            select(Execution, occurred.label("occurred_at")).where(condition)
            .order_by(occurred.desc(), Execution.id.desc()).limit(remaining)
        ).all()
        items.extend(
            TodayExecution(id=e.id, execution_type=e.execution_type, status=e.status, reason=reason,
                           occurred_at=occurred_at)
            for e, occurred_at in rows
        )
    return ExecutionSection(
        status="ok",
        attention=ExecutionList(items=items, total=total, truncated=total > EXECUTION_LIMIT),
    )


# ---------------------------------------------------------------- aggregation

_SECTIONS: tuple[tuple[str, type, Callable], ...] = (
    ("tasks", TaskSection, _tasks),
    ("reminders", ReminderSection, _reminders),
    ("notifications", NotificationSection, _notifications),
    ("executions", ExecutionSection, _executions),
)


def _read_section(db: Session, name: str, section_type: type, read: Callable, window: DayWindow):
    """单个 section 的数据库错误只影响该 section；非数据库异常原样向上传播。"""
    try:
        return read(db, window)
    except SQLAlchemyError as exc:
        logger.warning("today.section_unavailable section=%s error=%s", name, type(exc).__name__)
        try:
            db.rollback()  # 重置只读会话，后续 section 可继续读取
        except SQLAlchemyError:
            pass
        return section_type(status="unavailable")


def build_today(db: Session, *, now: datetime, timezone_name: str, zone: zoneinfo.ZoneInfo) -> TodayResponse:
    """计算本地日窗口并读取四个 section。只读：不 add / flush / commit。"""
    window = compute_day_window(now, zone)
    sections = {
        name: _read_section(db, name, section_type, read, window)
        for name, section_type, read in _SECTIONS
    }
    unavailable = sum(section.status == "unavailable" for section in sections.values())
    logger.info("today.projection timezone_valid=true sections_ok=%d sections_unavailable=%d",
                len(sections) - unavailable, unavailable)
    if unavailable == len(sections):
        raise TodayUnavailableError()
    return TodayResponse(
        timezone=timezone_name,
        local_date=window.local_date,
        generated_at=now,
        day_start=window.day_start,
        day_end=window.day_end,
        **sections,
    )
