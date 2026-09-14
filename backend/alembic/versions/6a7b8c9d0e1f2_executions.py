"""executions: Phase 6A Execution Domain Foundation 表

独立执行任务领域（与 Task 待办完全分离）：可追溯、可重试、可取消的
执行单元。设计要点（对应 Phase 6A 需求与架构设计 §6.1）：
- 幂等唯一约束 UNIQUE(owner_id, execution_type, idempotency_key)：
  并发重复请求的数据库级最终裁决（服务层 IntegrityError 路径）；
- (status, run_at) 复合索引：6B Worker 轮询候选（沿
  ix_reminders_status_remind_at 模式）；
- attempt_count / max_attempts / lease / retry 字段按已确认设计一次性
  建全（6B 不在本表加列，迁移链稳定）；本阶段无执行逻辑，仅持久化；
- retry_of_id 自引用 FK（6E retry 语义，ON DELETE SET NULL）；
- 双方言通用：无方言分支（与 FTS/pgvector 的方言表不同，本表纯通用）。

downgrade：drop 全部索引与表（可逆）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "e6f5d4c3b2a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "executions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("execution_type", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("normalized_payload_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(50), nullable=True),
        sa.Column("last_error_message", sa.String(500), nullable=True),
        sa.Column("retry_of_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["retry_of_id"], ["executions.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_executions_id", "executions", ["id"])
    op.create_index("ix_executions_status", "executions", ["status"])
    op.create_index(
        "ix_executions_status_run_at", "executions", ["status", "run_at"]
    )
    op.create_index(
        "uq_executions_idempotency",
        "executions",
        ["owner_id", "execution_type", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_executions_idempotency", table_name="executions")
    op.drop_index("ix_executions_status_run_at", table_name="executions")
    op.drop_index("ix_executions_status", table_name="executions")
    op.drop_index("ix_executions_id", table_name="executions")
    op.drop_table("executions")
