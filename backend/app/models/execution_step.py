"""ExecutionStep 模型（Phase 6C：Step Orchestration and Tool Safety）。

execution 的线性步骤序列，每步一行。行是步骤的【当前状态】；跨 attempt
的历史（每次失败/重试/重放）由 execution_events 不可变事件流保留——绝不用
覆写旧步骤的方式隐藏历史（SUCCEEDED 步骤恢复时无条件跳过，永不重跑）。

设计要点：
- UNIQUE(execution_id, step_index)：step_index 来自注册表模板的位置
  （0 基），稳定确定、不随重试变化；数据库约束保证同 execution 内索引唯一；
- UNIQUE(execution_id, step_key)：模板内 step_key 唯一（数据库级裁决）；
- step 行由 Worker 编排器惰性物化（首次接触该步骤时创建），也允许
  confirmation 服务在 Worker 执行前物化（grant 可在首次 attempt 之前）；
- attempt_id：最后一次运行该步骤的 attempt（可空；重试/重放时更新为
  新 attempt；attempt 行删除时 SET NULL——step 审计记录必须存活）；
- input_hash：规范化输入的 sha256——confirmation 的绑定指纹。grant 之后
  输入哈希发生变化 → 旧确认立即失效（EXPIRED），绝不跨 hash 复用；
- requires_confirmation 镜像工具注册时声明的策略（行内留档，审计用）；
- 敏感数据策略：input_json 只存规范化输入（截断上限 2000 字符，
  完整输入不落盘）；output_json 写入前必须经过 sanitization；
  error_message 经 sanitize_error_message 且上限 500 字符。
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class StepStatus(str, Enum):
    PENDING = "PENDING"  # 已物化、尚未开始
    RUNNING = "RUNNING"  # 正在执行（Worker 崩溃恢复时按 replay 策略裁决）
    SUCCEEDED = "SUCCEEDED"  # 成功完成（恢复时跳过，绝不重跑）
    FAILED = "FAILED"  # 失败（可按 attempt retry 机制在下一 attempt 重试）


class ConfirmationStatus(str, Enum):
    PENDING = "PENDING"  # 等待确认
    GRANTED = "GRANTED"  # 已确认（绑定 input_hash，hash 变化即失效）
    REJECTED = "REJECTED"  # 已拒绝（步骤进入稳定失败，不重试）
    EXPIRED = "EXPIRED"  # 旧确认失效（输入变化；需重新确认）


class ExecutionStep(Base):
    __tablename__ = "execution_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False
    )
    # 最后一次运行该步骤的 attempt（可空；SET NULL——step 审计记录存活于 attempt）
    attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="SET NULL"), nullable=True
    )
    step_key: Mapped[str] = mapped_column(String(50), nullable=False)
    # 模板内位置（0 基），稳定确定——重试/重放绝不改变
    step_index: Mapped[int] = mapped_column(Integer, nullable=False)
    tool_name: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=StepStatus.PENDING.value
    )
    # 规范化输入（截断存储，绝不保存完整敏感输入）；sha256 指纹单独一列
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 工具注册时声明的策略镜像（行内留档：该步骤按什么策略被执行/被确认）
    requires_confirmation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    confirmation_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confirmation_actor_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confirmation_actor_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # 稳定确定性索引唯一：step_index / step_key 均不得重复（数据库级裁决）
        Index(
            "uq_execution_steps_execution_index",
            "execution_id",
            "step_index",
            unique=True,
        ),
        Index(
            "uq_execution_steps_execution_key",
            "execution_id",
            "step_key",
            unique=True,
        ),
    )
