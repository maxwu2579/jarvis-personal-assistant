"""ExecutionEvent 模型（Phase 6C：不可变审计事件流）。

execution 生命周期内每个可审计时刻一行（append-only）。"immutable" 的准确
边界：**应用层 append-only**——服务层只暴露 append_event（无任何 UPDATE /
DELETE 路径），配合 UNIQUE(execution_id, sequence_number) 审计约束保证
序列不重叠、不覆写；不声称数据库管理员也无法修改（无 DB 触发器）。

设计要点：
- sequence_number 在事务内以 MAX+1 产生（同一事务可见未提交事件），
  UNIQUE 约束是并发下的最终裁决（冲突 → EXEC_EVENT_CONFLICT，事务回滚）；
- 事件只在状态实际变更的事务内写入（如 transition 的 rowcount=1 之后），
  回滚的事件随事务一起消失——事件与状态变更原子；
- payload 写入前必须经过 sanitization（sanitize_result_value）；
- attempt_id / step_id 可空（服务层事件无 attempt/step 上下文时为空）；
  均为 SET NULL——事件审计记录必须存活；
- 事件类型白名单（APPEND_ONLY_EVENT_TYPES）在 services/execution_events.py，
  模型本身不做枚举约束（迁移保持双方言通用）。
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ExecutionEvent(Base):
    __tablename__ = "execution_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_attempts.id", ondelete="SET NULL"), nullable=True
    )
    step_id: Mapped[int | None] = mapped_column(
        ForeignKey("execution_steps.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # 同 execution 内单调递增（事务内 MAX+1 + UNIQUE 约束兜底并发）
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 已脱敏 JSON 字符串（sanitize_result_value 后序列化）
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        # 审计约束：同 execution 的序列号唯一——事件不可覆写、不可重复
        Index(
            "uq_execution_events_execution_sequence",
            "execution_id",
            "sequence_number",
            unique=True,
        ),
    )
