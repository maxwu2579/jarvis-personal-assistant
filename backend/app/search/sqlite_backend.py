"""SQLite SearchBackend：FTS5 全文检索 + vector_json/Python cosine 向量检索。

设计（Phase 4B 行为不变的迁移目标 + 5B 零依赖向量路径）：
- keyword：FTS5 虚拟表 + BM25（原 fts5.py 逻辑，行为不变）；
- vector：chunk_embeddings.vector_json + Python cosine Top-K。
  * 原 retrieval_service._load_vector_candidates / _parse_vector_json /
    _stable_order_by_detail 全部逻辑原样移入本实现（行为不变，
    包括候选上限保护 RetrievalCandidateLimitExceededError）；
  * 向量已 L2 normalize（provider 保证），cosine == dot product；
  * 非法 vector_json（非数组/维度不符/非有限数）→ 跳过并记录日志，
    绝不当作可信向量使用，也不崩溃；
  * 排序稳定：(-score, document_id, chunk_index)，同分顺序确定；
- 所有查询参数绑定；document_ids 以占位符拼接，绝不内联用户输入。

这是「Python cosine」路径的唯一事实实现；PG 分支见
postgresql_backend.py（pgvector 原生 cosine）。
"""

import json
import logging
import math

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.task import utcnow
from app.search.base import SearchBackend
from app.services import fts5
from app.services.keyword_tokens import build_cjk_grams
from app.services.retrieval_errors import RetrievalCandidateLimitExceededError

logger = logging.getLogger(__name__)

_VECTOR_BACKEND_NAME = "python-cosine"


class SQLiteSearchBackend(SearchBackend):
    """SQLite 分支：FTS5 + BM25 / vector_json + Python cosine。"""

    backend_name = "fts5"
    vector_backend_name = _VECTOR_BACKEND_NAME

    # ---- keyword（FTS5）----

    def ensure_keyword_index(self, db: Session) -> None:
        fts5.ensure_fts_table(db)

    def write_keyword_rows(
        self, db: Session, document_id: int, rows: list[tuple[int, str]]
    ) -> None:
        # rows: [(chunk_id, content)] → 索引侧预生成 cjk_grams 后整体重写
        fts5.replace_document_rows(
            db,
            document_id,
            [
                (chunk_id, content, build_cjk_grams(content))
                for chunk_id, content in rows
            ],
        )

    def delete_keyword_rows(self, db: Session, document_id: int) -> None:
        fts5.delete_document_rows(db, document_id)

    def keyword_search(
        self,
        db: Session,
        *,
        query: str,
        document_ids: list[int] | None,
        limit: int,
    ) -> list[dict]:
        match_expr = fts5.build_match_expression(query)
        if match_expr is None:
            return []
        return fts5.keyword_search(
            db, match_expr=match_expr, document_ids=document_ids, limit=limit
        )

    # ---- vector（vector_json + Python cosine）----

    def existing_vector_rows(
        self, db: Session, document_id: int
    ) -> dict[int, dict]:
        rows = db.execute(
            text(
                "SELECT ce.chunk_id, ce.provider, ce.model, ce.dimension, "
                "ce.content_sha256 FROM chunk_embeddings ce "
                "JOIN document_chunks dc ON dc.id = ce.chunk_id "
                "WHERE dc.document_id = :doc_id"
            ),
            {"doc_id": document_id},
        ).mappings().all()
        return {
            row["chunk_id"]: {
                "provider": row["provider"],
                "model": row["model"],
                "dimension": row["dimension"],
                "content_sha256": row["content_sha256"],
            }
            for row in rows
        }

    def write_vector_rows(
        self,
        db: Session,
        document_id: int,  # noqa: ARG002 - 调用方显式携带，SQLite 按 chunk_id 写即可
        rows: list[tuple[int, list[float], str, str, int, str]],
    ) -> None:
        # rows: [(chunk_id, vector, provider, model_name, dimension,
        #         content_sha256)] → 按 chunk_id 幂等 upsert（一对一约束）
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        from app.models.document import ChunkEmbedding

        stmt = sqlite_insert(ChunkEmbedding).values(
            [
                {
                    "chunk_id": chunk_id,
                    "vector_json": json.dumps(vector, separators=(",", ":")),
                    "provider": provider,
                    "model": model_name,
                    "dimension": dimension,
                    "content_sha256": content_sha256,
                    "created_at": utcnow(),
                    "updated_at": utcnow(),
                }
                for chunk_id, vector, provider, model_name, dimension, content_sha256
                in rows
            ]
        )
        db.execute(
            stmt.on_conflict_do_update(
                index_elements=["chunk_id"],
                set_={
                    "vector_json": stmt.excluded.vector_json,
                    "provider": stmt.excluded.provider,
                    "model": stmt.excluded.model,
                    "dimension": stmt.excluded.dimension,
                    "content_sha256": stmt.excluded.content_sha256,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
        )

    def delete_vector_rows(self, db: Session, document_id: int) -> None:
        db.execute(
            text(
                "DELETE FROM chunk_embeddings WHERE chunk_id IN ("
                "SELECT id FROM document_chunks WHERE document_id = :doc_id)"
            ),
            {"doc_id": document_id},
        )

    def delete_vector_rows_by_chunk_ids(
        self, db: Session, chunk_ids: list[int]
    ) -> None:
        if not chunk_ids:
            return
        placeholders = ", ".join(f":cid_{i}" for i in range(len(chunk_ids)))
        params = {f"cid_{i}": cid for i, cid in enumerate(chunk_ids)}
        db.execute(
            text(
                f"DELETE FROM chunk_embeddings WHERE chunk_id IN ({placeholders})"
            ),
            params,
        )

    def orphaned_vector_chunk_ids(self, db: Session) -> list[int]:
        """全局孤儿向量行 chunk_id：向量行存在但其 chunk 已不存在。"""
        rows = db.execute(
            text(
                "SELECT ce.chunk_id FROM chunk_embeddings ce "
                "LEFT JOIN document_chunks dc ON dc.id = ce.chunk_id "
                "WHERE dc.id IS NULL"
            )
        ).mappings().all()
        return [row["chunk_id"] for row in rows]

    def any_stale_vector_rows(
        self,
        db: Session,
        provider: str,
        model: str,
        dimension: int,
    ) -> bool:
        row = db.execute(
            text(
                "SELECT 1 FROM chunk_embeddings "
                "WHERE provider != :p OR model != :m OR dimension != :d LIMIT 1"
            ),
            {"p": provider, "m": model, "d": dimension},
        ).scalar()
        return row == 1

    def vector_search(
        self,
        db: Session,
        *,
        query_vector: list[float],
        document_ids: list[int] | None,
        limit: int,
        identity: tuple[str, str, int] | None = None,
    ) -> tuple[list[dict], int]:
        rows = self._load_vector_candidates(db, document_ids, identity)
        total_candidates = len(rows)
        scored: list[tuple[float, int, int, int]] = []  # (score, doc_id, chunk_index, chunk_id)
        for row in rows:
            vector = _parse_vector_json(
                row["vector_json"], len(query_vector), row["chunk_id"]
            )
            if vector is None:
                continue  # 跳过非法向量（记录日志，不崩溃）
            # 向量已 L2 normalize：cosine == dot product（维度已校验一致）
            score = sum(a * b for a, b in zip(query_vector, vector))
            scored.append((score, row["document_id"], row["chunk_index"], row["chunk_id"]))
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        top = scored[:limit]
        return (
            [
                {"chunk_id": chunk_id, "document_id": document_id, "score": score}
                for score, document_id, _chunk_index, chunk_id in top
            ],
            total_candidates,
        )

    def count_vector_rows(
        self, db: Session, document_ids: list[int]
    ) -> dict[int, int]:
        if not document_ids:
            return {}
        placeholders = ", ".join(f":doc_{i}" for i in range(len(document_ids)))
        params = {f"doc_{i}": doc_id for i, doc_id in enumerate(document_ids)}
        rows = db.execute(
            text(
                "SELECT dc.document_id, COUNT(ce.chunk_id) AS n "
                "FROM chunk_embeddings ce "
                "JOIN document_chunks dc ON dc.id = ce.chunk_id "
                f"WHERE dc.document_id IN ({placeholders}) "
                "GROUP BY dc.document_id"
            ),
            params,
        ).mappings().all()
        return {row["document_id"]: row["n"] for row in rows}

    # ---- 内部 ----

    def _load_vector_candidates(
        self,
        db: Session,
        document_ids: list[int] | None,
        identity: tuple[str, str, int] | None = None,
    ) -> list[dict]:
        """加载候选 embedding（全扫描，Python 计算相似度）。

        候选上限保护：超限报错（提示未来 pgvector），不假装能大规模。
        identity（Phase 5C）：只加载当前有效身份的向量行，绝不混入
        stale（provider/model/dimension 任一过期的旧行）；None = 不过滤。
        """
        sql = (
            "SELECT ce.chunk_id, ce.vector_json, dc.document_id, dc.chunk_index "
            "FROM chunk_embeddings ce "
            "JOIN document_chunks dc ON dc.id = ce.chunk_id "
            "JOIN documents d ON d.id = dc.document_id "
            "WHERE d.index_status = 'INDEXED'"
        )
        params: dict = {}
        if identity is not None:
            sql += (
                " AND ce.provider = :vp AND ce.model = :vm "
                "AND ce.dimension = :vd"
            )
            params["vp"], params["vm"], params["vd"] = identity
        if document_ids:
            placeholders = ", ".join(f":doc_{i}" for i in range(len(document_ids)))
            sql += f" AND d.id IN ({placeholders})"
            params.update({f"doc_{i}": doc_id for i, doc_id in enumerate(document_ids)})
        rows = db.execute(text(sql), params).mappings().all()
        total = len(rows)
        if total > settings.retrieval_candidate_limit:
            raise RetrievalCandidateLimitExceededError(
                total, settings.retrieval_candidate_limit
            )
        return [dict(row) for row in rows]


def _parse_vector_json(
    raw: str, expected_dimension: int, chunk_id: int
) -> list[float] | None:
    """严格验证 vector_json：JSON array、长度等于 dimension、全部有限 number。

    任何不合法返回 None（调用方跳过并记录日志）——绝不当作可信向量使用。
    """
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("retrieval.invalid_vector_json chunk_id=%s", chunk_id)
        return None
    if not isinstance(parsed, list) or len(parsed) != expected_dimension:
        logger.warning("retrieval.vector_dimension_mismatch chunk_id=%s", chunk_id)
        return None
    for value in parsed:
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            logger.warning("retrieval.vector_non_finite chunk_id=%s", chunk_id)
            return None
    return [float(value) for value in parsed]
