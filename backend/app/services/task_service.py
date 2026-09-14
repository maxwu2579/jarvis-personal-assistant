"""任务领域逻辑。

职责：状态机与数据读写。不感知 HTTP —— 业务失败抛出领域异常，
由 API 层映射为稳定的 HTTP 状态码与错误码。
"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.models.task import Task, TaskStatus
from app.services import reminder_service


class TaskNotFoundError(Exception):
    """任务不存在。映射为 404。"""

    def __init__(self, task_id: int):
        self.task_id = task_id
        super().__init__(f"Task {task_id} does not exist")


class InvalidTransitionError(Exception):
    """非法状态转换。映射为 409。"""

    def __init__(self, task_id: int, action: str, current: TaskStatus, allowed: str):
        self.task_id = task_id
        self.action = action
        self.current = current
        self.allowed = allowed
        super().__init__(
            f"Task {task_id} cannot be {action}: current status is {current.value}, "
            f"only {allowed} is allowed"
        )


# 允许的状态转换表：action -> {当前状态: 目标状态}
ALLOWED_TRANSITIONS: dict[str, dict[TaskStatus, TaskStatus]] = {
    "confirm": {TaskStatus.DRAFT: TaskStatus.CONFIRMED},
    "complete": {TaskStatus.CONFIRMED: TaskStatus.DONE},
}


def create_task(
    db: Session,
    *,
    title: str,
    description: str | None,
    due_at: datetime | None,
    commit: bool = True,
) -> Task:
    """创建任务：永远生成 DRAFT，不产生任何日历 / 提醒副作用。

    commit=False 用于与其他写入合并为同一事务（如 Proposal 流程）；
    调用方负责最终 commit/rollback。
    """
    task = Task(
        title=title,
        description=description,
        status=TaskStatus.DRAFT.value,
        due_at=due_at,
    )
    db.add(task)
    if commit:
        db.commit()
        db.refresh(task)
    return task


def list_tasks(db: Session) -> list[Task]:
    return db.query(Task).order_by(Task.created_at.desc()).all()


def get_task(db: Session, task_id: int) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise TaskNotFoundError(task_id)
    return task


def confirm_task(db: Session, task_id: int) -> Task:
    """确认草稿：DRAFT -> CONFIRMED。

    同一事务内：若存在 due_at，创建 PENDING Reminder（remind_at=due_at）；
    无 due_at 不创建。重复 confirm 由状态机 409 拒绝，不会创建第二个 Reminder
    （活动提醒部分唯一索引作为最终防重）。
    """
    task = _transition(db, task_id, "confirm", commit=False)
    reminder_service.schedule_on_confirm(db, task)
    db.commit()
    db.refresh(task)
    return task


def complete_task(db: Session, task_id: int) -> Task:
    """完成已确认任务：CONFIRMED -> DONE。

    同一事务内：取消未投递的 PENDING Reminder（DELIVERED 历史与 Notification 保留）。
    """
    task = _transition(db, task_id, "complete", commit=False)
    reminder_service.cancel_on_complete(db, task)
    db.commit()
    db.refresh(task)
    return task


def postpone_task(db: Session, task_id: int, due_at: datetime) -> Task:
    """延期：只允许 CONFIRMED 状态修改 due_at（DRAFT 尚未确认、DONE 已完成）。

    同一事务内：同步更新 PENDING Reminder 的 remind_at（CLAIMED/DELIVERED 不修改）。
    """
    task = get_task(db, task_id)
    if TaskStatus(task.status) is not TaskStatus.CONFIRMED:
        raise InvalidTransitionError(
            task_id, "postponed", TaskStatus(task.status), TaskStatus.CONFIRMED.value
        )
    task.due_at = due_at
    reminder_service.reschedule_on_postpone(db, task)
    db.commit()
    db.refresh(task)
    return task


def _transition(db: Session, task_id: int, action: str, *, commit: bool = True) -> Task:
    task = get_task(db, task_id)
    transitions = ALLOWED_TRANSITIONS[action]
    current = TaskStatus(task.status)
    if current not in transitions:
        allowed = ", ".join(s.value for s in transitions)
        raise InvalidTransitionError(task_id, action, current, allowed)
    task.status = transitions[current].value
    if commit:
        db.commit()
        db.refresh(task)
    return task
