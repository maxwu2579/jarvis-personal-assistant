"""proposal rejected audit

Revision ID: a1b2c3d4e5f6
Revises: 66405497a397
Create Date: 2026-08-11

REJECTED 审计：assistant_message_id / task_id / action / arguments_json / explanation
改为可空（REJECTED 记录不创建任务、不伪造 ASSISTANT、不保存原始输出），
新增 error_code / error_detail 审计列与 status 索引。
SQLite 修改可空性需要 batch 模式（重建表，旧数据复制保留）。

Phase 5A 跨方言修正（已发布迁移，修改原因见 README「迁移兼容」）：
- SQLite 分支保持 batch（既有行为与历史库完全一致，revision id 未变）；
- PostgreSQL 分支走原生 ALTER COLUMN（batch 模式仅 SQLite 支持，
  PG 上执行会硬失败）；语义与 batch 完全等价。
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '66405497a397'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_sqlite() -> bool:
    """在线/离线（--sql）模式都可用：离线时 bind 的 dialect 来自 URL。"""
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    if _is_sqlite():
        # SQLite：修改可空性只能 batch（重建表，旧数据复制保留）
        with op.batch_alter_table('task_proposals') as batch_op:
            batch_op.alter_column('assistant_message_id', existing_type=sa.Integer(), nullable=True)
            batch_op.alter_column('task_id', existing_type=sa.Integer(), nullable=True)
            batch_op.alter_column('action', existing_type=sa.String(length=50), nullable=True)
            batch_op.alter_column('arguments_json', existing_type=sa.Text(), nullable=True)
            batch_op.alter_column('explanation', existing_type=sa.Text(), nullable=True)
            batch_op.add_column(sa.Column('error_code', sa.String(length=50), nullable=True))
            batch_op.add_column(sa.Column('error_detail', sa.String(length=500), nullable=True))
            batch_op.create_index('ix_task_proposals_status', ['status'])
    else:
        # PostgreSQL：原生 ALTER COLUMN（batch 仅 SQLite 支持；语义等价）
        op.alter_column('task_proposals', 'assistant_message_id', existing_type=sa.Integer(), nullable=True)
        op.alter_column('task_proposals', 'task_id', existing_type=sa.Integer(), nullable=True)
        op.alter_column('task_proposals', 'action', existing_type=sa.String(length=50), nullable=True)
        op.alter_column('task_proposals', 'arguments_json', existing_type=sa.Text(), nullable=True)
        op.alter_column('task_proposals', 'explanation', existing_type=sa.Text(), nullable=True)
        op.add_column('task_proposals', sa.Column('error_code', sa.String(length=50), nullable=True))
        op.add_column('task_proposals', sa.Column('error_detail', sa.String(length=500), nullable=True))
        op.create_index('ix_task_proposals_status', 'task_proposals', ['status'])


def downgrade() -> None:
    if _is_sqlite():
        with op.batch_alter_table('task_proposals') as batch_op:
            batch_op.drop_index('ix_task_proposals_status')
            batch_op.drop_column('error_detail')
            batch_op.drop_column('error_code')
            batch_op.alter_column('explanation', existing_type=sa.Text(), nullable=False)
            batch_op.alter_column('arguments_json', existing_type=sa.Text(), nullable=False)
            batch_op.alter_column('action', existing_type=sa.String(length=50), nullable=False)
            batch_op.alter_column('task_id', existing_type=sa.Integer(), nullable=False)
            batch_op.alter_column('assistant_message_id', existing_type=sa.Integer(), nullable=False)
    else:
        op.drop_index('ix_task_proposals_status', table_name='task_proposals')
        op.drop_column('task_proposals', 'error_detail')
        op.drop_column('task_proposals', 'error_code')
        op.alter_column('task_proposals', 'explanation', existing_type=sa.Text(), nullable=False)
        op.alter_column('task_proposals', 'arguments_json', existing_type=sa.Text(), nullable=False)
        op.alter_column('task_proposals', 'action', existing_type=sa.String(length=50), nullable=False)
        op.alter_column('task_proposals', 'task_id', existing_type=sa.Integer(), nullable=False)
        op.alter_column('task_proposals', 'assistant_message_id', existing_type=sa.Integer(), nullable=False)
