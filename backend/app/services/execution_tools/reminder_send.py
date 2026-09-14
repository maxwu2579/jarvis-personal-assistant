"""reminder.send 执行工具 handler（Phase 7.1）。

在 Execution 执行域内复用既有 Reminder 投递逻辑（reminder_service._deliver）：
- 复用语义：创建一条对应应用内 Notification、reminder 状态 → DELIVERED、
  唯一约束幂等（重复投递视为已投递）、可重试/永久失败分类——全部沿用
  Reminder Worker 的既有实现，不复制、不改写、不另起投递通道
  （无 Email / SMS / 微信 / 外部网络）；
- 不调用 _claim_and_deliver：那是 Reminder Worker 的领取路径
  （attempt_count / lease），与 Execution 域的 attempt / step 裁决互不干扰；
- 事务边界：本 handler 的业务副作用（Notification / reminder 状态）在
  自己的 session 中独立原子提交；执行域编排器（execution_steps）不感知
  该事务，step / event / lease 写入由编排器另行管控——两套写入互不越过
  对方的原子边界；
- session 与时钟通过模块级 _session_factory / _clock 注入（默认
  app.core.database.SessionLocal / SystemClock）：handler 契约
  (payload) -> dict 无 db 参数，测试用 monkeypatch 替换注入测试库与
  固定时钟，生产路径零配置；
- 错误语义：reminder 不存在或 task_id 不匹配 → EXEC_REMINDER_NOT_FOUND；
  _deliver 返回 "claimed"（可重试数据库错误）→
  EXEC_REMINDER_DELIVERY_RETRYABLE；"failed"（永久数据错误）→
  EXEC_REMINDER_DELIVERY_FAILED。是否重试由执行域 attempt 裁决
  （ToolSpec retryable=True），本 handler 不决策。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.clock import SystemClock
from app.core.database import SessionLocal
from app.models.reminder import Reminder
from app.services.execution_errors import ExecutionError
from app.services.reminder_service import _deliver

# 可注入边界（测试 monkeypatch 替换；生产为零配置）：
# - _session_factory：handler 业务事务的 session 来源；
# - _clock：投递时间戳来源（与执行域 get_clock 同语义，aware UTC）。
_session_factory = SessionLocal
_clock = SystemClock()


class ReminderSendInput(BaseModel):
    """reminder.send 输入（extra="forbid"，白名单字段）。

    字段语义：reminder_id 定位 Reminder；task_id 必须与 Reminder 归属
    一致（不一致即拒绝，绝不投递给错误任务）；message 作为执行上下文
    回显（输出可见、落盘经 sanitize），不改变既有投递正文语义。
    """

    model_config = ConfigDict(extra="forbid")

    reminder_id: int
    task_id: int
    message: str


class ReminderSendOutput(BaseModel):
    """reminder.send 输出（extra="forbid"；落盘前再经 sanitize_result_value）。"""

    model_config = ConfigDict(extra="forbid")

    status: str
    reminder_id: int
    task_id: int
    message: str


def reminder_send_handler(input_value: dict[str, Any]) -> dict[str, Any]:
    reminder_id = int(input_value["reminder_id"])
    task_id = int(input_value["task_id"])
    db = _session_factory()
    try:
        reminder = db.get(Reminder, reminder_id)
        if reminder is None:
            raise ExecutionError(
                "EXEC_REMINDER_NOT_FOUND",
                f"reminder {reminder_id} not found",
            )
        if reminder.task_id != task_id:
            raise ExecutionError(
                "EXEC_REMINDER_NOT_FOUND",
                f"reminder {reminder_id} does not belong to task {task_id}",
            )
        outcome = _deliver(db, reminder, _clock.now())
    except ExecutionError:
        db.rollback()
        raise
    finally:
        db.close()

    if outcome != "delivered":
        code = (
            "EXEC_REMINDER_DELIVERY_RETRYABLE"
            if outcome == "claimed"
            else "EXEC_REMINDER_DELIVERY_FAILED"
        )
        raise ExecutionError(code, f"reminder delivery outcome {outcome!r}")
    return {
        "status": "delivered",
        "reminder_id": reminder_id,
        "task_id": task_id,
        "message": input_value["message"],
    }
