"""execution_steps + execution_events: Phase 6C Step Orchestration & Tool Safety

execution_steps（步骤编排）：
- 每 execution 一行一步（线性模板，注册表静态声明）；行是步骤的当前状态，
  跨 attempt 历史由 execution_events 不可变事件流保留；
- UNIQUE(execution_id, step_index)：模板内位置（0 基）稳定确定，数据库级
  裁决（并发物化只有一方成功）；UNIQUE(execution_id, step_key)：模板键唯一；
- attempt_id 外键 ON DELETE SET NULL——attempt 删除时步骤审计记录必须存活；
- input_hash（sha256）是 confirmation 的绑定指纹：grant 后输入变化 → 旧确认
  立即失效（EXPIRED，服务层裁决），绝不跨 hash 复用；
- input_json 只存规范化输入（服务层截断上限 2000 字符，完整敏感输入不落盘）；
  output_json 写入前必须经 sanitization；error_message 上限 500 字符。

execution_events（不可变审计事件流）：
- 应用层 append-only：服务层无 UPDATE/DELETE 路径（无 DB 触发器——不声称
  DB 管理员也无法修改，"immutable" 指应用层契约 + 审计约束）；
- UNIQUE(execution_id, sequence_number)：序列不重叠、不覆写——并发下的最终
  裁决（冲突 → EXEC_EVENT_CONFLICT，事务回滚，事件与状态变更原子消失）；
- attempt_id / step_id 均 ON DELETE SET NULL——审计记录必须存活；
- payload 写入前必须经 sanitization（敏感字段名 → <redacted>）。

双方言通用（纯通用表，无方言分支）；downgrade 可逆（只移除 6C 对象，
既有表保留）。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "7b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "execution_steps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("execution_id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=True),
        sa.Column("step_key", sa.String(50), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("tool_name", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("output_json", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(50), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False),
        sa.Column("confirmation_status", sa.String(20), nullable=True),
        sa.Column("confirmation_actor_type", sa.String(20), nullable=True),
        sa.Column("confirmation_actor_id", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["execution_attempts.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_execution_steps_id", "execution_steps", ["id"])
    op.create_index(
        "uq_execution_steps_execution_index",
        "execution_steps",
        ["execution_id", "step_index"],
        unique=True,
    )
    op.create_index(
        "uq_execution_steps_execution_key",
        "execution_steps",
        ["execution_id", "step_key"],
        unique=True,
    )
    op.create_index(
        "ix_execution_steps_execution_id", "execution_steps", ["execution_id"]
    )

    op.create_table(
        "execution_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("execution_id", sa.Integer(), nullable=False),
        sa.Column("attempt_id", sa.Integer(), nullable=True),
        sa.Column("step_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"], ["executions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["attempt_id"], ["execution_attempts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["step_id"], ["execution_steps.id"], ondelete="SET NULL"
        ),
    )
    op.create_index("ix_execution_events_id", "execution_events", ["id"])
    op.create_index(
        "uq_execution_events_execution_sequence",
        "execution_events",
        ["execution_id", "sequence_number"],
        unique=True,
    )
    op.create_index(
        "ix_execution_events_execution_id", "execution_events", ["execution_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_execution_events_execution_id", table_name="execution_events")
    op.drop_index(
        "uq_execution_events_execution_sequence", table_name="execution_events"
    )
    op.drop_index("ix_execution_events_id", table_name="execution_events")
    op.drop_table("execution_events")
    op.drop_index("ix_execution_steps_execution_id", table_name="execution_steps")
    op.drop_index("uq_execution_steps_execution_key", table_name="execution_steps")
    op.drop_index("uq_execution_steps_execution_index", table_name="execution_steps")
    op.drop_index("ix_execution_steps_id", table_name="execution_steps")
    op.drop_table("execution_steps")
