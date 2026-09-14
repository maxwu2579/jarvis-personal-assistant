"""pgvector vectors: PostgreSQL 原生向量表 + HNSW cosine 索引（Phase 5B）

设计（对应 Phase 5B「pgvector 数据模型与迁移」）：
- 独立 PostgreSQL 专属表 pg_chunk_vectors（**不进 SQLAlchemy Base.metadata**，
  与 FTS5 虚拟表同模式）：SQLite 的 create_all / 迁移永不编译 VECTOR 类型，
  SQLite 路径继续使用 chunk_embeddings.vector_json；
- chunk_id 一对一主键 + FK(document_chunks.id) ON DELETE CASCADE：
  删除文档/块时向量行自动级联清理；
- embedding VECTOR({settings.embedding_dimension}) 固定维度：
  已建表后仅改 EMBEDDING_DIMENSION 环境变量 → 运行时稳定报错
  VECTOR_DIMENSION_MISMATCH（见 services/vector_readiness.py），
  绝不自动截断/填充/转换；
- model_name / dimension / provider / content_sha256 随行持久化（可审计；
  换 model/dimension 后旧的向量行被显式 reindex 覆盖）；
- HNSW cosine 索引（vector_cosine_ops）：默认参数 m=16 /
  ef_construction=64（pgvector 官方默认）。选择 HNSW 而非 IVFFlat 的原因：
  IVFFlat 建索引前需要足够训练数据做聚类（本项目当前小规模语料不满足），
  HNSW 无需训练、小数据集上召回稳定，实现即用；
  零向量无法进入 HNSW cosine 索引（余弦距离未定义）——由写入路径显式拒绝
  （整体回滚 + INDEX_FAILED），见 retrieval_index_service；
- 不在 downgrade 中 DROP EXTENSION：extension 是数据库级共享对象，
  可能被库内其他对象使用，删除破坏面过大；生命周期边界见 README。

SQLite 分支：upgrade/downgrade 均为无操作（不创建扩展、不建表、不建索引）。
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'e6f5d4c3b2a1'
down_revision: Union[str, Sequence[str], None] = '7c9d4e2f5a1b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 索引名稳定明确（integration 测试与运维直接引用）
HNSW_INDEX = "ix_pg_chunk_vectors_embedding_hnsw"
TABLE = "pg_chunk_vectors"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    """Upgrade schema."""
    if not _is_postgresql():
        # SQLite 分支：无操作（保持 vector_json 路径与零依赖）
        return

    # 1. extension：权限不足或扩展不可用 → 迁移明确失败（脱敏文案，
    #    不泄漏连接串；原始异常作为 cause 保留在日志）
    try:
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    except Exception as exc:
        raise RuntimeError(
            "pgvector extension could not be created on the PostgreSQL server; "
            "install the pgvector extension or grant CREATE EXTENSION permission "
            "(see README 'Phase 5B')"
        ) from exc

    # 2. 固定维度 vector 列（维度来自应用配置；schema 与实际维度的一致性
    #    由运行时检查强制，见 services/vector_readiness.py）
    from app.core.config import settings

    dim = settings.embedding_dimension
    op.execute(
        f"CREATE TABLE {TABLE} ("
        "chunk_id INTEGER PRIMARY KEY "
        "REFERENCES document_chunks(id) ON DELETE CASCADE, "
        f"embedding VECTOR({dim}) NOT NULL, "
        "provider VARCHAR(50) NOT NULL, "
        "model_name VARCHAR(100) NOT NULL, "
        "dimension INTEGER NOT NULL, "
        "content_sha256 VARCHAR(64) NOT NULL, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL)"
    )

    # 3. HNSW cosine 索引（默认参数；零向量由写入路径显式拒绝）
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {HNSW_INDEX} "
        f"ON {TABLE} USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    if not _is_postgresql():
        return
    op.execute(f"DROP INDEX IF EXISTS {HNSW_INDEX}")
    op.execute(f"DROP TABLE IF EXISTS {TABLE}")
    # 不 DROP EXTENSION：数据库级共享对象，可能被其他对象使用
    # （extension 生命周期边界见 README 'Phase 5B'）
