"""检索索引服务（Phase 4B + 5B）：把 READY 文档的 chunks 同步为
向量行 + 关键词索引行，并驱动 Document 检索状态机。

状态机（服务层独占转移，模型只落合法值）：
    NOT_INDEXED -> INDEXING -> INDEXED | INDEX_FAILED

事务边界：
- 状态机推进是独立 commit（INDEXING 可观测）；
- 向量行 + 关键词索引 + INDEXED 在单事务内原子提交；任何失败整体
  rollback，不留下半套向量/索引数据，不把文档退回摄取 FAILED
  （解析成功与索引失败是不同概念，原始 chunks 不受影响）；
- 重试幂等（Phase 5C stale 状态机）：内容与身份全等
  （sha+provider+model+dimension）→ 跳过（skipped）；身份未变、内容
  变化 → 更新（updated）；身份过期（provider/model/dimension 任一与
  当前 provider 不一致）→ stale 替换（单独计数）；新 chunk → 新增
  （created）；孤儿向量行（chunk 已不存在的残留）事务内全局清理
  （deleted_orphans 计数）；
  关键词索引行始终整体重写（SQLite=FTS5 先删后插，无幽灵记录；
  PG=兼容 ILIKE 回退，无索引表可写，见 SearchBackend）；
- 强制重建（reindex）：先显式清空旧向量行（方言无关，通过
  SearchBackend.delete_vector_rows），再全部重建。

向量写入（Phase 5B）全部通过 SearchBackend 方言接缝：
- SQLite → chunk_embeddings.vector_json（upsert，行为与 4B 完全一致）；
- PG → pg_chunk_vectors 原生 vector 列（upsert，零向量显式拒绝）；
- 批量 embedding：按 settings.embedding_batch_max 分批调用 provider
  （外部 provider 有批量上限；顺序与输入严格一致）；
- _embed_chunk_batch 是模块级函数（测试可注入失败/替身）。

错误语义：
- 文档不存在 → DOCUMENT_NOT_FOUND（404）；非 READY → DOCUMENT_NOT_READY（409）；
- 关键词索引能力不可用（SQLite 无 FTS5）→ RETRIEVAL_NOT_AVAILABLE（503，
  环境能力问题，不改文档状态）；
- PG 向量就绪检查失败（缺 extension/表/维度不匹配）→ 稳定 RetrievalError
  （503）→ 回滚 + INDEX_FAILED + sanitized 错误 + 重抛 INDEXING_FAILED；
- 嵌入/写入失败 → 回滚 + INDEX_FAILED + sanitized 错误 + 重抛
  INDEXING_FAILED（500，不吞异常）。
"""

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.embeddings.base import EmbeddingInternalError, EmbeddingProvider
from app.embeddings.factory import create_embedding_provider
from app.models.document import (
    Document,
    DocumentChunk,
    DocumentIndexStatus,
    DocumentStatus,
)
from app.search.factory import get_search_backend
from app.services.document_errors import DocumentNotFoundError
from app.services.retrieval_errors import (
    DocumentNotReadyError,
    IndexingFailedError,
)

logger = logging.getLogger(__name__)


@dataclass
class IndexStats:
    """一次索引操作的受控统计（API 响应与日志使用，不含敏感数据）。

    Phase 5C 计数语义：
    - embeddings_created：新增向量行（此前无向量行的 chunk）；
    - embeddings_updated：内容变化、身份未变的重算（sha 变化）；
    - embeddings_skipped：内容与身份（sha/provider/model/dimension）
      全等 → 跳过；
    - embeddings_stale：身份过期（provider/model/dimension 任一与当前
      不一致）被替换的旧身份向量行数；
    - deleted_orphans：本次清理的孤儿向量行数（向量行存在但其 chunk
      已不存在，如 SQLite FK pragma 未启用时的历史残留）；
    - failures：批量索引中失败的文档数。
    """

    documents_scanned: int = 0
    chunks_scanned: int = 0
    embeddings_created: int = 0
    embeddings_updated: int = 0
    embeddings_skipped: int = 0
    embeddings_stale: int = 0
    deleted_orphans: int = 0
    fts_rows_written: int = 0
    failures: int = 0

    def to_dict(self) -> dict:
        return {
            "documents_scanned": self.documents_scanned,
            "chunks_scanned": self.chunks_scanned,
            "embeddings_created": self.embeddings_created,
            "embeddings_updated": self.embeddings_updated,
            "embeddings_skipped": self.embeddings_skipped,
            "embeddings_stale": self.embeddings_stale,
            "deleted_orphans": self.deleted_orphans,
            "fts_rows_written": self.fts_rows_written,
            "failures": self.failures,
        }


def _content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _embed_chunk_batch(
    provider: EmbeddingProvider, contents: list[str]
) -> list[list[float]]:
    """批量生成 chunk 向量（模块级函数：测试可注入失败/替身）。

    按 provider.max_batch_size 分批（Phase 5C：批量上限由 provider 声明，
    外部 provider 可收紧；默认取配置），顺序与输入严格一致；
    空列表返回空列表。
    """
    if not contents:
        return []
    vectors: list[list[float]] = []
    batch_size = provider.max_batch_size
    for start in range(0, len(contents), batch_size):
        vectors.extend(provider.embed_documents(contents[start : start + batch_size]))
    return vectors


class RetrievalIndexService:
    def __init__(self, db: Session, provider: EmbeddingProvider | None = None):
        self.db = db
        self.provider = provider or create_embedding_provider()
        self.backend = get_search_backend()

    # ---- 对外操作 ----

    def index_document(self, document_id: int) -> IndexStats:
        """幂等索引：内容/provider/model/dimension 未变则跳过。"""
        document = self._require_ready(document_id)
        self.backend.ensure_keyword_index(self.db)
        stats = IndexStats(documents_scanned=1)
        return self._index_transaction(document, stats, force=False)

    def reindex_document(self, document_id: int) -> IndexStats:
        """强制重建：先清空旧向量与 FTS 行，再全部重建。"""
        document = self._require_ready(document_id)
        self.backend.ensure_keyword_index(self.db)
        stats = IndexStats(documents_scanned=1)
        return self._index_transaction(document, stats, force=True)

    def delete_document_index(self, document_id: int) -> None:
        """删除文档的检索索引（幂等：未索引时无操作）。"""
        document = self.db.get(Document, document_id)
        if document is None:
            return
        self.backend.delete_keyword_rows(self.db, document_id)
        self.backend.delete_vector_rows(self.db, document_id)
        document.index_status = DocumentIndexStatus.NOT_INDEXED.value
        document.index_error_message = None
        self.db.commit()

    def index_ready_documents(self, limit: int | None = None) -> IndexStats:
        """批量索引全部 READY 文档（受控服务方法，不暴露为无条件重建 API）。

        单个文档失败不中断批量：failures 计数 + 日志（显式容错采集，
        不是静默吞异常——错误信息进入统计与日志）。
        """
        query = select(Document.id).where(
            Document.status == DocumentStatus.READY.value,
            Document.index_status.in_(
                [
                    DocumentIndexStatus.NOT_INDEXED.value,
                    DocumentIndexStatus.INDEX_FAILED.value,
                ]
            ),
        ).order_by(Document.id.asc())
        if limit is not None:
            query = query.limit(limit)
        ids = [row[0] for row in self.db.execute(query).all()]
        total = IndexStats(documents_scanned=len(ids))
        for document_id in ids:
            try:
                partial = self.index_document(document_id)
                total.chunks_scanned += partial.chunks_scanned
                total.embeddings_created += partial.embeddings_created
                total.embeddings_updated += partial.embeddings_updated
                total.embeddings_skipped += partial.embeddings_skipped
                total.embeddings_stale += partial.embeddings_stale
                total.deleted_orphans += partial.deleted_orphans
                total.fts_rows_written += partial.fts_rows_written
            except Exception as exc:  # noqa: BLE001 - 批量容错采集
                total.failures += 1
                logger.warning(
                    "retrieval.index_document_failed id=%s error=%s",
                    document_id,
                    getattr(exc, "message", str(exc)),
                )
        return total

    # ---- 内部实现 ----

    def _require_ready(self, document_id: int) -> Document:
        document = self.db.get(Document, document_id)
        if document is None:
            raise DocumentNotFoundError(document_id)
        if DocumentStatus(document.status) is not DocumentStatus.READY:
            raise DocumentNotReadyError(document_id, document.status)
        return document

    def _index_transaction(
        self, document: Document, stats: IndexStats, *, force: bool
    ) -> IndexStats:
        document_id = document.id
        # 状态推进是独立 commit：INDEXING 可观测（前端显示「索引中」）
        document.index_status = DocumentIndexStatus.INDEXING.value
        document.index_error_message = None
        self.db.commit()

        try:
            chunks = (
                self.db.query(DocumentChunk)
                .filter(DocumentChunk.document_id == document_id)
                .order_by(DocumentChunk.chunk_index.asc())
                .all()
            )
            stats.chunks_scanned = len(chunks)

            if force:
                # 强制重建：显式清空旧向量行（方言无关；FTS 行在 replace
                # 时整体重写，PG 关键词回退无索引表）
                self.backend.delete_vector_rows(self.db, document_id)
                existing = {}
            else:
                existing = self.backend.existing_vector_rows(self.db, document_id)

            # Phase 5C：稳定 provider 标识（provider.provider_name，
            # 不再用类名——openai 配置名与 openai-compatible 标识分离）
            provider_name = self.provider.provider_name
            model = self.provider.model_name
            dimension = self.provider.dimension

            # 孤儿清理（Phase 5C）：向量行存在但其 chunk 已不存在
            # （SQLite FK pragma 未启用 / 重摄 chunk 集合收缩的历史残留）。
            # 事务内原子执行：任何失败整体回滚（含向量写入）。
            orphan_ids = self.backend.orphaned_vector_chunk_ids(self.db)
            if orphan_ids:
                self.backend.delete_vector_rows_by_chunk_ids(self.db, orphan_ids)
                stats.deleted_orphans = len(orphan_ids)

            # 决定哪些 chunk 需要（重新）embedding。Phase 5C stale 语义：
            # - 身份全等（sha+provider+model+dimension）→ 跳过（skipped）；
            # - 身份未变、内容变化 → 更新（updated）；
            # - 身份过期（provider/model/dimension 任一不同）→ stale
            #   （旧身份向量行被替换，计数单独统计）；
            # - 无旧行 → 新建（created）。
            needs_embedding: list[tuple[int, str, str]] = []  # (chunk_id, content, sha)
            for chunk in chunks:
                sha = _content_sha256(chunk.content)
                prev = existing.get(chunk.id)
                if prev is None or force:
                    if prev is None:
                        stats.embeddings_created += 1
                    needs_embedding.append((chunk.id, chunk.content, sha))
                    continue
                identity_unchanged = (
                    prev["provider"] == provider_name
                    and prev["model"] == model
                    and prev["dimension"] == dimension
                )
                if identity_unchanged and prev["content_sha256"] == sha:
                    stats.embeddings_skipped += 1
                    continue
                if identity_unchanged:
                    stats.embeddings_updated += 1
                else:
                    stats.embeddings_stale += 1
                needs_embedding.append((chunk.id, chunk.content, sha))

            if needs_embedding:
                vectors = _embed_chunk_batch(
                    self.provider, [content for _, content, _ in needs_embedding]
                )
                # 零向量统一拒绝（Phase 5C）：服务层前置校验，SQLite 与
                # PG 行为一致；backend 层保留同款防御（HNSW 无法表示零向量）
                for chunk_id, vector in zip(
                    (item[0] for item in needs_embedding), vectors
                ):
                    if not any(value != 0.0 for value in vector):
                        raise EmbeddingInternalError(
                            "zero vector cannot be stored in the vector index"
                        )
                self.backend.write_vector_rows(
                    self.db,
                    document_id,
                    [
                        (chunk_id, vector, provider_name, model, dimension, sha)
                        for (chunk_id, _content, sha), vector in zip(
                            needs_embedding, vectors
                        )
                    ],
                )

            # 关键词索引：整体重写（SQLite=FTS5 先删后插，无幽灵记录；
            # PG=兼容 ILIKE 回退，无索引表，无操作）
            self.backend.write_keyword_rows(
                self.db,
                document_id,
                [(chunk.id, chunk.content) for chunk in chunks],
            )
            stats.fts_rows_written = len(chunks)

            document.index_status = DocumentIndexStatus.INDEXED.value
            document.index_error_message = None
            self.db.commit()
        except Exception as exc:
            # 整体回滚：不留下半套向量/FTS 数据
            self.db.rollback()
            message = self._sanitize_error(exc)
            document.index_status = DocumentIndexStatus.INDEX_FAILED.value
            document.index_error_message = message[:500]
            self.db.commit()
            logger.warning(
                "retrieval.index_failed id=%s status=%s",
                document_id,
                document.status,
            )
            raise IndexingFailedError(document_id, message) from exc
        return stats

    @staticmethod
    def _sanitize_error(exc: Exception) -> str:
        """sanitized 错误简述：已知稳定错误直接取 message，其余固定文案。"""
        from app.embeddings.base import EmbeddingError
        from app.services.retrieval_errors import RetrievalError

        if isinstance(exc, (EmbeddingError, RetrievalError)):
            return exc.message
        return "indexing failed"
