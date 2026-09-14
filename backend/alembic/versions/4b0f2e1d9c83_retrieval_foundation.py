"""retrieval foundation: chunk_embeddings + document index_status + FTS5

Phase 4B 迁移：
1. documents 增加检索索引状态列（index_status / index_error_message）；
2. 新表 chunk_embeddings（chunk_id 唯一 + FK CASCADE，持久化向量）；
3. FTS5 虚拟表 document_chunks_fts（普通虚拟表，非 external-content /
   contentless：行数据自包含、rowid=chunk_id、可 INSERT OR REPLACE 幂等重写；
   由 retrieval_index_service 在事务内显式同步，无自动 trigger——
   触发器的隐式行为难以测试，显式同步逻辑清晰且可故障注入）。

FTS 表不进 SQLAlchemy metadata（create_all 不建它）：虚拟表在迁移中创建，
运行期由索引服务确保存在（CREATE VIRTUAL TABLE IF NOT EXISTS）。
SQLite 不支持 FTS5 时本迁移会响亮失败（不会假装成功）。

Phase 5A 跨方言修正（已发布迁移，修改原因见 README「迁移兼容」）：
- FTS5 虚拟表只创建于 SQLite 分支；PostgreSQL 分支绝不执行
  CREATE VIRTUAL TABLE（语法错误），PG 检索走兼容 ILIKE 回退
  （Phase 5A compatibility fallback，非 pgvector；5B 再替换）。

Revision ID: 4b0f2e1d9c83
Revises: 8f2a4c1e9b37
Create Date: 2026-08-16 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4b0f2e1d9c83'
down_revision: Union[str, Sequence[str], None] = '8f2a4c1e9b37'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# FTS 表名（与 retrieval 服务中的单一来源常量保持一致）
FTS_TABLE = "document_chunks_fts"


def upgrade() -> None:
    """Upgrade schema."""
    # ---- documents：检索索引状态（NOT_INDEXED 默认，存量行安全）----
    op.add_column(
        'documents',
        sa.Column(
            'index_status',
            sa.String(length=20),
            nullable=False,
            server_default='NOT_INDEXED',
        ),
    )
    op.add_column(
        'documents',
        sa.Column('index_error_message', sa.String(length=500), nullable=True),
    )
    op.create_index(
        op.f('ix_documents_index_status'), 'documents', ['index_status'], unique=False
    )

    # ---- chunk_embeddings：chunk 持久化向量 ----
    op.create_table(
        'chunk_embeddings',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('chunk_id', sa.Integer(), nullable=False),
        sa.Column('provider', sa.String(length=50), nullable=False),
        sa.Column('model', sa.String(length=100), nullable=False),
        sa.Column('dimension', sa.Integer(), nullable=False),
        sa.Column('vector_json', sa.Text(), nullable=False),
        sa.Column('content_sha256', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['chunk_id'], ['document_chunks.id'], ondelete='CASCADE'
        ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('chunk_id', name='uq_chunk_embeddings_chunk_id'),
        sqlite_autoincrement=True,
    )
    op.create_index(op.f('ix_chunk_embeddings_id'), 'chunk_embeddings', ['id'], unique=False)
    op.create_index(
        op.f('ix_chunk_embeddings_chunk_id'), 'chunk_embeddings', ['chunk_id'], unique=False
    )

    # ---- FTS5 虚拟表（仅 SQLite 分支）----
    # 普通虚拟表：content（原文）+ cjk_grams（中文 n-gram 辅助列）可搜索，
    # document_id 为 UNINDEXED 列（用于文档过滤）。rowid = chunk_id
    # （INSERT OR REPLACE 保证重建幂等、无幽灵重复行）。
    # PG 分支不创建 FTS5（语法不支持；检索走兼容 ILIKE 回退，见模块 docstring）。
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5("
            "content, "
            "cjk_grams, "
            "document_id UNINDEXED"
            ")"
        )
    # ### end Alembic commands ###


def downgrade() -> None:
    """Downgrade schema."""
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
    op.drop_index(op.f('ix_chunk_embeddings_chunk_id'), table_name='chunk_embeddings')
    op.drop_index(op.f('ix_chunk_embeddings_id'), table_name='chunk_embeddings')
    op.drop_table('chunk_embeddings')
    op.drop_index(op.f('ix_documents_index_status'), table_name='documents')
    op.drop_column('documents', 'index_error_message')
    op.drop_column('documents', 'index_status')
    # ### end Alembic commands ###
