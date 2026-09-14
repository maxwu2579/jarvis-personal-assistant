"""任务 API 路由。业务异常在这里统一映射为稳定的 HTTP 状态码。"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.task import TaskCreate, TaskOut, TaskPostpone
from app.services import task_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.post("", response_model=TaskOut, status_code=201, summary="创建草稿任务")
def create_task(payload: TaskCreate, db: Session = Depends(get_db)):
    return task_service.create_task(
        db, title=payload.title, description=payload.description, due_at=payload.due_at
    )


@router.get("", response_model=list[TaskOut], summary="任务列表")
def list_tasks(db: Session = Depends(get_db)):
    return task_service.list_tasks(db)


@router.get("/{task_id}", response_model=TaskOut, summary="获取单个任务")
def get_task(task_id: int, db: Session = Depends(get_db)):
    return task_service.get_task(db, task_id)


@router.post("/{task_id}/confirm", response_model=TaskOut, summary="确认草稿（DRAFT -> CONFIRMED）")
def confirm_task(task_id: int, db: Session = Depends(get_db)):
    return task_service.confirm_task(db, task_id)


@router.post("/{task_id}/complete", response_model=TaskOut, summary="完成任务（CONFIRMED -> DONE）")
def complete_task(task_id: int, db: Session = Depends(get_db)):
    return task_service.complete_task(db, task_id)


@router.patch("/{task_id}/postpone", response_model=TaskOut, summary="延期（仅 CONFIRMED 可修改 due_at）")
def postpone_task(task_id: int, payload: TaskPostpone, db: Session = Depends(get_db)):
    return task_service.postpone_task(db, task_id, payload.due_at)
