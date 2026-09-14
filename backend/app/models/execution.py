"""Execution 数据模型（Phase 6A：Execution Domain Foundation）。

独立于 Task（用户待办清单）的执行任务领域：Task 是 DRAFT/CONFIRMED/DONE
待办状态机，Execution 是可追溯、可重试、可取消的执行单元。两者表、枚举、
错误码完全分离（ExecutionStatus / EXEC_* 前缀），绝不复用 Task 语义。

状态机（9 态）：
    CREATED → QUEUED → CLAIMED → RUNNING → SUCCEEDED
                                │            ├─→ FAILED
              └─→ CANCELLED     │            └─→ TIMED_OUT
    QUEUED/CLAIMED/RUNNING → CANCEL_REQUESTED → CANCELLED / SUCCEEDED / FAILED
    终态（SUCCEEDED/FAILED/CANCELLED/TIMED_OUT）不可逆，无任何出边。

并发与幂等设计：
- UNIQUE(owner_id, execution_type, idempotency_key)：同 owner + 操作类型 +
  幂等键的唯一约束是并发重复请求的最终裁决（绝不只靠先查后写）；
- 规范化 payload 哈希（normalized_payload_hash）：同 key 同请求 → 重放；
  同 key 不同请求 → 409 冲突（服务层裁决）；
- 时间语义：所有时间字段 UTC。本阶段（6A）只持久化，不实现
  claim/lease/retry/backoff/timeout 执行逻辑（6B）、不实现调度（6D）、
  不提供 HTTP API（6E）。attempt_count/max_attempts/lease 等列按
  已确认设计一次性建全，避免后续阶段改动本表迁移。
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.task import utcnow


class ExecutionStatus(str, Enum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"


# 终态集合：不可逆，无任何出边（服务层转换白名单据此拒绝）
TERMINAL_STATUSES: frozenset[ExecutionStatus] = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.CANCELLED,
        ExecutionStatus.TIMED_OUT,
    }
)


class Execution(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # 幂等作用域之一：请求者标识。单用户应用默认 "default"（6E API 层传入）
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # 类型注册表白名单内（services/execution_registry.py）；6A 仅 system.ping
    execution_type: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=ExecutionStatus.CREATED.value, index=True
    )
    # 规范化后的 JSON 字符串（键已排序；仅含 registry 白名单字段）
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    # sha256(规范化 JSON) —— 幂等重放/冲突裁决的请求指纹
    normalized_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # 单次调度时刻（仅存储；调度逻辑属 6D）
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # ---- 6B+ 预留字段：本阶段仅持久化，无执行逻辑 ----
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 6E retry 产生的新 execution 对原 execution 的引用（自引用 FK，SET NULL）
    retry_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("executions.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        # 幂等唯一约束：并发重复请求的数据库级最终裁决（owner + type + key）
        Index(
            "uq_executions_idempotency",
            "owner_id",
            "execution_type",
            "idempotency_key",
            unique=True,
        ),
        # 6B Worker 轮询候选：QUEUED 且 run_at 到期（沿 ix_reminders_status_remind_at 模式）
        Index("ix_executions_status_run_at", "status", "run_at"),
        {"sqlite_autoincrement": True},
    )
