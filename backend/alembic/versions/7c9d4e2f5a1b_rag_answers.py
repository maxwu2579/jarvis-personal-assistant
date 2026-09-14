"""rag_answers: RAG 回答审计表

Phase 4C 迁移：新增 rag_answers 审计表，为每条 /api/rag/ask 提供可追溯的
结构化记录（检索模式、文档/块快照、引用标签、使用量、状态与错误码）。
消息表不动：引用关联通过 user_message_id / assistant_message_id 外键
（ON DELETE SET NULL）实现，不向 messages 塞无法查询的 JSON 自由文本。

设计要点：
- 允许独立使用（conversation_id 可空）；
- 结构化 JSON 列可查询（document_ids / chunk_ids / citation_labels）；
- 不存完整 system prompt、API Key、原始异常、模型原始输出；
- downgrade 干净移除（无历史数据依赖：审计行随表删除）。

Revision ID: 7c9d4e2f5a1b
Revises: 4b0f2e1d9c83
Create Date: 2026-08-16 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c9d4e2f5a1b'
down_revision: Union[str, Sequence[str], None] = '4b0f2e1d9c83'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'rag_answers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('conversation_id', sa.Integer(), nullable=True),
        sa.Column('user_message_id', sa.Integer(), nullable=True),
        sa.Column('assistant_message_id', sa.Integer(), nullable=True),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('language', sa.String(length=10), nullable=False),
        sa.Column('retrieval_mode', sa.String(length=20), nullable=False),
        sa.Column('document_ids_json', sa.Text(), nullable=True),
        sa.Column('chunk_ids_json', sa.Text(), nullable=True),
        sa.Column('citation_labels_json', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('error_code', sa.String(length=50), nullable=True),
        sa.Column('model', sa.String(length=100), nullable=True),
        sa.Column('prompt_tokens', sa.Integer(), nullable=True),
        sa.Column('completion_tokens', sa.Integer(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('candidate_count', sa.Integer(), nullable=False),
        sa.Column('returned_count', sa.Integer(), nullable=False),
        sa.Column('context_chars', sa.Integer(), nullable=False),
        sa.Column('context_token_estimate', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['assistant_message_id'], ['messages.id'], ondelete='SET NULL'
        ),
        sa.ForeignKeyConstraint(
            ['conversation_id'], ['conversations.id'], ondelete='CASCADE'
        ),
        sa.ForeignKeyConstraint(
            ['user_message_id'], ['messages.id'], ondelete='SET NULL'
        ),
        sa.PrimaryKeyConstraint('id'),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f('ix_rag_answers_id'), 'rag_answers', ['id'], unique=False)
    op.create_index(
        op.f('ix_rag_answers_conversation_id'),
        'rag_answers',
        ['conversation_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_rag_answers_user_message_id'),
        'rag_answers',
        ['user_message_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_rag_answers_assistant_message_id'),
        'rag_answers',
        ['assistant_message_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_rag_answers_status'), 'rag_answers', ['status'], unique=False
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_rag_answers_status'), table_name='rag_answers')
    op.drop_index(
        op.f('ix_rag_answers_assistant_message_id'), table_name='rag_answers'
    )
    op.drop_index(op.f('ix_rag_answers_user_message_id'), table_name='rag_answers')
    op.drop_index(
        op.f('ix_rag_answers_conversation_id'), table_name='rag_answers'
    )
    op.drop_index(op.f('ix_rag_answers_id'), table_name='rag_answers')
    op.drop_table('rag_answers')
    # ### end Alembic commands ###
