"""execution_attempts: Phase 6B Durable Execution Worker 尝试表

每次尝试一行：claim 即创建，retry 只插入新编号行，旧 attempt 作为审计
记录保留（绝不覆写）。设计要点（对应 Phase 6B 需求）：
- UNIQUE(execution_id, attempt_number)：attempt 编号由数据库约束保证唯一；
- attempt 持有租约（worker_id / lease_token / lease_expires_at 权威字段），
  executions 表的 lease 三列是当前活跃 attempt 的镜像（claim/renew 同事务
  同步写，见 services/execution_claim.py）；
- execution_id 外键 ON DELETE CASCADE（execution 删除时 attempt 随删）；
- (status) 与 (lease_expires_at) 索引：租约回收扫描（活跃态 + 过期）走
  索引范围查询；
- 双方言通用：无方言分支（与 FTS/pgvector 的方言表不同，本表纯通用）。

attempt 状态（AttemptStatus）：CLAIMED / RUNNING / SUCCEEDED / FAILED /
ABORTED。SUCCEEDED/FAILED/ABORTED 为终态（回收/超时/取消只对活跃态动手）。

downgrade：drop 全部索引与表（可逆）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7b8c9d0e1f2a3"
down_revision: Union[str, Sequence[str], None] = "6a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "execution_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("execution_id", sa.Integer(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("lease_token", sa.String(64), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="CASCADE"
        ),
    )
    op.create_index("ix_execution_attempts_id", "execution_attempts", ["id"])
    op.create_index(
        "uq_execution_attempts_number",
        "execution_attempts",
        ["execution_id", "attempt_number"],
        unique=True,
    )
    op.create_index(
        "ix_execution_attempts_execution_id",
        "execution_attempts",
        ["execution_id"],
    )
    op.create_index(
        "ix_execution_attempts_status", "execution_attempts", ["status"]
    )
    op.create_index(
        "ix_execution_attempts_lease_expires_at",
        "execution_attempts",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_execution_attempts_lease_expires_at", table_name="execution_attempts"
    )
    op.drop_index("ix_execution_attempts_status", table_name="execution_attempts")
    op.drop_index(
        "ix_execution_attempts_execution_id", table_name="execution_attempts"
    )
    op.drop_index(
        "uq_execution_attempts_number", table_name="execution_attempts"
    )
    op.drop_index("ix_execution_attempts_id", table_name="execution_attempts")
    op.drop_table("execution_attempts")
