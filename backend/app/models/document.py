"""Document / DocumentChunk / ChunkEmbedding 数据模型（Phase 4A + 4B）。

Document 状态机（由 service 层控制，模型只落合法状态）：
    UPLOADED -> PROCESSING -> READY | FAILED

- UPLOADED：文件已通过校验并原子落盘，DB 行已提交；
- PROCESSING：解析与切块进行中（独立事务）；
- READY：解析成功，chunks 已落库（chunk_count 可由 chunks 表推导）；
- FAILED：解析/切块失败，error_code/error_message 记录可审计的稳定原因
  （sanitized：不含绝对路径、堆栈、文件内容片段）。

Document 检索索引状态机（Phase 4B，独立于摄取状态，由索引服务控制）：
    NOT_INDEXED -> INDEXING -> INDEXED | INDEX_FAILED
文档解析成功（READY）与检索索引失败（INDEX_FAILED）是不同概念：
索引失败绝不把文档退回 FAILED，原始 chunks 不受影响。

磁盘与数据库的一致性约定：存储键为 UUID hex（无扩展名），文件内容与
原始文件名完全解耦；删除时先删 DB 行（成功后）再清理磁盘文件。

ChunkEmbedding（4B）：chunk 的持久化向量。向量与 FTS 索引由
retrieval_index_service 在明确事务边界内同步（无自动 trigger）。
"""

from datetime import datetime
from enum import Enum

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.task import utcnow


class DocumentStatus(str, Enum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class DocumentIndexStatus(str, Enum):
    """检索索引状态（服务层独占状态转移，模型只落合法值）。"""

    NOT_INDEXED = "NOT_INDEXED"
    INDEXING = "INDEXING"
    INDEXED = "INDEXED"
    INDEX_FAILED = "INDEX_FAILED"


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # 仅用于展示的原始文件名（不可信输入；不参与任何路径计算）
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # 服务端记录的 Content-Type（来自上传请求；与扩展名不一致的伪装由校验拒绝）
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    # 原始字节 sha256（十六进制）。明确不做内容去重：相同内容每次上传
    # 都是独立 Document（策略见 README），sha256 仅用于审计与比对。
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # 磁盘存储名：UUID hex（32 字符），无扩展名；杜绝路径注入与类型混淆
    storage_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=DocumentStatus.UPLOADED.value, index=True
    )
    # 处理失败时的稳定错误码与简述（sanitized，不保存路径/堆栈/内容）
    error_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # 检索索引状态（Phase 4B）：与摄取状态独立，服务层独占转移
    index_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=DocumentIndexStatus.NOT_INDEXED.value,
        index=True,
    )
    # 索引失败时的 sanitized 简述（不保存路径/堆栈/向量内容）
    index_error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_documents_storage_key"),
        {"sqlite_autoincrement": True},
    )


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 文档内从 0 开始的顺序；(document_id, chunk_index) 唯一保证块序确定
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 原始文本中的字符偏移区间 [char_start, char_end)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)
    # 近似 token 数：ceil(字符数 / 4)，中文为主的保守估算
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "document_id", "chunk_index", name="uq_document_chunks_document_index"
        ),
        {"sqlite_autoincrement": True},
    )


class ChunkEmbedding(Base):
    """chunk 的持久化向量（Phase 4B 检索基础）。

    设计决策：
    - chunk_id 唯一：一个 chunk 至多一条向量（防止重复生成）；
    - provider / model / dimension 持久化：向量语义随它们变化，
      不能只存在配置里（换 provider/维度时必须重新索引）；
    - content_sha256：判断 chunk 内容是否变化（内容不变则跳过重算）；
    - vector_json：JSON 数组文本。读取后必须严格验证（JSON array、
      长度等于 dimension、全部有限 number），任何不合法都按跳过处理，
      绝不当作可信向量使用；
    - 日志与错误响应中绝不输出完整向量。
    """

    __tablename__ = "chunk_embeddings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    chunk_id: Mapped[int] = mapped_column(
        ForeignKey("document_chunks.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    vector_json: Mapped[str] = mapped_column(Text, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    __table_args__ = ({"sqlite_autoincrement": True},)
