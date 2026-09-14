"""Reminder / Notification 只读 API 与已读操作。

安全边界：
- 不提供任何创建/修改 Reminder 的公共接口（Reminder 只能由 Task confirm/postpone/
  complete 的副作用产生，或由 Worker 投递）；
- 不提供绕过 Task confirm 的隐藏路径；
- 标记已读是幂等操作。
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.clock import Clock, get_clock
from app.core.database import get_db
from app.models.reminder import Notification, Reminder
from app.schemas.reminder import NotificationOut, ReminderOut, UnreadCountOut
from app.services.reminder_service import (
    CANCELLED,
    CLAIMED,
    DELIVERED,
    FAILED,
    PENDING,
)

router = APIRouter(tags=["reminders"])

REMINDER_STATUSES = {PENDING, CLAIMED, DELIVERED, CANCELLED, FAILED}


def _get_reminder_or_404(db: Session, reminder_id: int) -> Reminder:
    reminder = db.get(Reminder, reminder_id)
    if reminder is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "REMINDER_NOT_FOUND", "message": f"Reminder {reminder_id} does not exist"}},
        )
    return reminder


def _get_notification_or_404(db: Session, notification_id: int) -> Notification:
    notification = db.get(Notification, notification_id)
    if notification is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOTIFICATION_NOT_FOUND", "message": f"Notification {notification_id} does not exist"}},
        )
    return notification


# 分页上限：列表必须可被分页读取，防止无界返回
MAX_PAGE_LIMIT = 200
DEFAULT_PAGE_LIMIT = 50


def _page_params(limit: int | None, offset: int | None) -> tuple[int, int]:
    """统一分页默认值；非法值（负数/超限）由路由签名的 Query 约束返回 422。"""
    return limit or DEFAULT_PAGE_LIMIT, offset or 0


@router.get("/api/reminders", response_model=list[ReminderOut], summary="Reminder 列表（可按状态/任务过滤，支持分页）")
def list_reminders(
    db: Session = Depends(get_db),
    status: str | None = Query(default=None, max_length=20),
    task_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_LIMIT),
    offset: int | None = Query(default=None, ge=0),
):
    page_limit, page_offset = _page_params(limit, offset)
    query = db.query(Reminder)
    if status is not None:
        if status not in REMINDER_STATUSES:
            raise HTTPException(
                status_code=422,
                detail={"error": {"code": "VALIDATION_ERROR", "message": f"invalid reminder status {status!r}"}},
            )
        query = query.filter(Reminder.status == status)
    if task_id is not None:
        query = query.filter(Reminder.task_id == task_id)
    return (
        query.order_by(Reminder.remind_at.asc(), Reminder.id.asc())
        .offset(page_offset)
        .limit(page_limit)
        .all()
    )


@router.get("/api/reminders/{reminder_id}", response_model=ReminderOut, summary="Reminder 详情")
def get_reminder(reminder_id: int, db: Session = Depends(get_db)):
    return _get_reminder_or_404(db, reminder_id)


@router.get("/api/notifications", response_model=list[NotificationOut], summary="通知列表（可按未读/任务过滤，支持分页）")
def list_notifications(
    db: Session = Depends(get_db),
    unread_only: bool = Query(default=False),
    task_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_LIMIT),
    offset: int | None = Query(default=None, ge=0),
):
    page_limit, page_offset = _page_params(limit, offset)
    query = db.query(Notification)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    if task_id is not None:
        query = query.filter(Notification.task_id == task_id)
    return (
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset(page_offset)
        .limit(page_limit)
        .all()
    )


@router.get("/api/notifications/unread-count", response_model=UnreadCountOut, summary="未读通知数量")
def unread_count(db: Session = Depends(get_db)):
    count = db.query(Notification).filter(Notification.read_at.is_(None)).count()
    return UnreadCountOut(count=count)


@router.post("/api/notifications/{notification_id}/read", response_model=NotificationOut, summary="标记通知已读（幂等）")
def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    clock: Clock = Depends(get_clock),
):
    notification = _get_notification_or_404(db, notification_id)
    if notification.read_at is None:
        # 可注入时钟：测试用 FixedClock 固定 read_at，生产用系统时钟
        notification.read_at = clock.now()
        db.commit()
        db.refresh(notification)
    return notification
