"""ExecutionAttempt 模型（Phase 6B：Durable Execution Worker）。

每次尝试（attempt）一行：claim 即创建，绝不覆写旧 attempt——旧 attempt
作为审计记录保留。attempt 持有 lease（worker_id / lease_token /
lease_expires_at 权威字段）；execution 上的 lease 三列是当前活跃 attempt
的镜像（claim/renew 同事务同步写，见 services/execution_claim.py）。

attempt 状态（AttemptStatus）：
- CLAIMED    claim 事务创建（execution 同步 CLAIMED，租约已登记）
- RUNNING    worker 开始执行（execution RUNNING，started_at 落盘）
- SUCCEEDED  执行成功（终态）
- FAILED     执行失败（可重试或耗尽 max_attempts，终态）
- ABORTED    租约丢失（回收/超时/取消，未完成，终态）

编号唯一：UNIQUE(execution_id, attempt_number) 由数据库约束保证——
retry 只能插入新编号行，任何路径都无法覆写已存在编号。
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class AttemptStatus(str, Enum):
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


# 终态 attempt 不可复写（回收/超时/取消扫描只对活跃态动手）
TERMINAL_ATTEMPT_STATUSES: frozenset[AttemptStatus] = frozenset(
    {AttemptStatus.SUCCEEDED, AttemptStatus.FAILED, AttemptStatus.ABORTED}
)

# 活跃态：持有租约（renew 只允许这些状态）
LIVE_ATTEMPT_STATUSES: frozenset[AttemptStatus] = frozenset(
    {AttemptStatus.CLAIMED, AttemptStatus.RUNNING}
)


class ExecutionAttempt(Base):
    __tablename__ = "execution_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("executions.id", ondelete="CASCADE"), nullable=False
    )
    # 数据库约束保证编号唯一（UNIQUE(execution_id, attempt_number)）
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_token: Mapped[str] = mapped_column(String(64), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
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
        # attempt 编号唯一：retry 只能新增编号，旧 attempt 不可被覆写
        Index(
            "uq_execution_attempts_number",
            "execution_id",
            "attempt_number",
            unique=True,
        ),
        Index("ix_execution_attempts_execution_id", "execution_id"),
        # 租约回收扫描：活跃态 + 过期
        Index("ix_execution_attempts_lease_expires_at", "lease_expires_at"),
    )
