"""PostgreSQL SearchBackend：兼容性关键词回退 + pgvector 原生向量检索（5A+5B）。

诚实边界：
- keyword 仍是 ILIKE 子串匹配（Phase 5A 兼容回退），**不是 PG FTS**；
- vector 是 pgvector 原生余弦 Top-K（Phase 5B）：
  * 表 pg_chunk_vectors（由 Alembic 迁移创建，不在 Base.metadata），
    embedding VECTOR(dim) + HNSW cosine 索引；
  * 查询 = ORDER BY (embedding <=> :q) ASC + LIMIT（数据库端 Top-K），
    score = 1 - distance（cosine similarity），越大越相关；
  * 无静默回退：extension/表/维度不满足 → 稳定 RetrievalError（503），
    绝不自动改用 Python 扫描或截断向量；
  * HNSW 是近似最近邻索引（召回质量由 pgvector 的 ef_search 决定）；
    ORDER BY 附加 document_id / chunk_index / chunk_id tie-breakers，
    保证同距离结果顺序确定、相同查询跨调用可复现
    （声明的是「稳定排序」，不是「与精确扫描完全一致」）；
  * 零向量无法进入 HNSW cosine 索引 → 写入路径显式拒绝（整体回滚）；
  * 写入幂等 upsert（ON CONFLICT chunk_id DO UPDATE）；
- 实现零 pgvector Python 依赖：本模块只写原生 SQL，
  向量参数绑定依赖 database.py 的 register_vector 连接适配器
  （psycopg3 按连接注册），SQLite 环境可安全导入本模块；
- 安全：用户输入（query / document_ids）只出现在绑定参数中，
  build_keyword_sql / build_vector_sql 是纯函数，测试直接验证。
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.embeddings.base import EmbeddingInternalError
from app.models.task import utcnow
from app.search.base import SearchBackend
from app.services.keyword_tokens import scan_query_tokens
from app.services.vector_readiness import raise_for_vector_readiness

logger = logging.getLogger(__name__)

# 单查询 token 上限：保护 SQL 长度与执行成本（超出截断——与 FTS5 一致）
MAX_QUERY_TOKENS = 50

_VECTOR_BACKEND_NAME = "pgvector-cosine"


def build_keyword_sql(
    query: str, document_ids: list[int] | None, limit: int
) -> tuple[str | None, dict]:
    """构造参数绑定的 ILIKE 查询（纯函数，便于单元测试）。

    返回 (sql, params)；无可搜索 token 时返回 (None, {})。
    用户输入只出现在 params 的绑定值中，绝不进入 SQL 文本。
    """
    tokens = scan_query_tokens(query)[:MAX_QUERY_TOKENS]
    if not tokens:
        return None, {}
    params: dict = {}
    cases: list[str] = []
    ors: list[str] = []
    for i, token in enumerate(tokens):
        key = f"t{i}"
        params[key] = f"%{token}%"
        cases.append(f"dc.content ILIKE :{key}")
        ors.append(f"dc.content ILIKE :{key}")
    # score：命中的 token 数（线性近似，非 BM25；详见模块 docstring）
    score_expr = " + ".join(f"CASE WHEN {c} THEN 1 ELSE 0 END" for c in cases)
    sql = (
        "SELECT dc.id AS chunk_id, dc.document_id, "
        f"({score_expr}) AS score "
        "FROM document_chunks dc "
        "JOIN documents d ON d.id = dc.document_id "
        "WHERE d.index_status = 'INDEXED' "
        f"AND ({' OR '.join(ors)})"
    )
    if document_ids:
        placeholders = ", ".join(f":doc_{i}" for i in range(len(document_ids)))
        sql += f" AND d.id IN ({placeholders})"
        params.update(
            {f"doc_{i}": doc_id for i, doc_id in enumerate(document_ids)}
        )
    sql += (
        " ORDER BY score DESC, dc.document_id ASC, dc.chunk_index ASC "
        "LIMIT :limit"
    )
    params["limit"] = min(limit, settings.retrieval_candidate_limit)
    return sql, params


def build_vector_sql(
    document_ids: list[int] | None,
    limit: int,
    identity: tuple[str, str, int] | None = None,
) -> tuple[str, dict]:
    """构造 pgvector cosine Top-K 查询（纯函数，便于单元测试）。

    返回 (sql, params)；查询向量绑定到 :q。
    - score = 1 - (embedding <=> CAST(:q AS vector))（cosine similarity，
      越大越相关）。psycopg3 客户端将 list 参数绑定为 double precision[]，
      而表达式上下文不会自动应用 array→vector 的 ASSIGNMENT cast，必须
      显式 CAST(:q AS vector)（INSERT 走赋值 cast 无需处理）；
    - 稳定排序：distance ASC, document_id ASC, chunk_index ASC, chunk_id ASC
      （同距离/同分时顺序确定，跨查询可复现）；
    - total = COUNT(*) OVER()：LIMIT 之前的全部合法候选数（与 SQLite
      全扫描语义一致）；document_ids 全部绑定，绝不内联用户输入；
    - 只查已索引文档（d.index_status = 'INDEXED'，与关键词路径一致）；
    - identity=(provider_name, model_name, dimension)：Phase 5C 身份过滤
      ——只检索当前有效身份的向量行，绝不混入 stale；None = 不过滤。
    """
    sql = (
        "SELECT pv.chunk_id AS chunk_id, dc.document_id AS document_id, "
        "(1 - (pv.embedding <=> CAST(:q AS vector))) AS score, "
        "COUNT(*) OVER() AS total "
        "FROM pg_chunk_vectors pv "
        "JOIN document_chunks dc ON dc.id = pv.chunk_id "
        "JOIN documents d ON d.id = dc.document_id "
        "WHERE d.index_status = 'INDEXED'"
    )
    params: dict = {}
    if identity is not None:
        sql += (
            " AND pv.provider = :vp AND pv.model_name = :vm "
            "AND pv.dimension = :vd"
        )
        params["vp"], params["vm"], params["vd"] = identity
    if document_ids:
        placeholders = ", ".join(f":doc_{i}" for i in range(len(document_ids)))
        sql += f" AND d.id IN ({placeholders})"
        params.update({f"doc_{i}": doc_id for i, doc_id in enumerate(document_ids)})
    sql += (
        " ORDER BY (pv.embedding <=> CAST(:q AS vector)) ASC, "
        "dc.document_id ASC, dc.chunk_index ASC, pv.chunk_id ASC "
        "LIMIT :limit"
    )
    params["limit"] = min(limit, settings.retrieval_candidate_limit)
    return sql, params


class PostgreSQLSearchBackend(SearchBackend):
    """PG 分支：ILIKE 兼容回退（keyword）+ pgvector 原生余弦（vector）。"""

    backend_name = "ilike-fallback"
    vector_backend_name = _VECTOR_BACKEND_NAME

    # ---- keyword（ILIKE 兼容回退）----

    def ensure_keyword_index(self, db: Session) -> None:
        # 无独立关键词索引表：查询直接扫描 document_chunks（ILIKE）。
        pass

    def write_keyword_rows(
        self, db: Session, document_id: int, rows: list[tuple[int, str]]
    ) -> None:
        # 无关键词索引表需要维护（向量路径由 pg_chunk_vectors 表承载）。
        pass

    def delete_keyword_rows(self, db: Session, document_id: int) -> None:
        pass

    def keyword_search(
        self,
        db: Session,
        *,
        query: str,
        document_ids: list[int] | None,
        limit: int,
    ) -> list[dict]:
        sql, params = build_keyword_sql(query, document_ids, limit)
        if sql is None:
            return []
        rows = db.execute(text(sql), params).mappings().all()
        return [
            {
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "score": float(row["score"]),
            }
            for row in rows
        ]

    # ---- vector（pgvector 原生余弦）----

    def existing_vector_rows(
        self, db: Session, document_id: int
    ) -> dict[int, dict]:
        rows = db.execute(
            text(
                "SELECT pv.chunk_id, pv.provider, pv.model_name AS model, "
                "pv.dimension, pv.content_sha256 "
                "FROM pg_chunk_vectors pv "
                "JOIN document_chunks dc ON dc.id = pv.chunk_id "
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
        document_id: int,  # noqa: ARG002 - 与接口一致；PG 按 chunk_id 写即可
        rows: list[tuple[int, list[float], str, str, int, str]],
    ) -> None:
        if not rows:
            return
        # 边界检查：extension/表/列维度 必须就绪，否则整体失败（无静默回退）
        raise_for_vector_readiness(db, rows[0][4])
        now = utcnow()
        params: list[dict] = []
        for chunk_id, vector, provider, model_name, dimension, content_sha256 in rows:
            # 零向量拒绝：HNSW cosine 索引无法表示零向量的余弦距离，
            # 写入会静默破坏召回；显式失败 → 调用方整体回滚（INDEX_FAILED）
            if not any(value != 0.0 for value in vector):
                raise EmbeddingInternalError(
                    "zero vector cannot be stored in the pgvector HNSW "
                    "cosine index"
                )
            params.append(
                {
                    "chunk_id": chunk_id,
                    "embedding": vector,
                    "provider": provider,
                    "model_name": model_name,
                    "dimension": dimension,
                    "content_sha256": content_sha256,
                    "created_at": now,
                    "updated_at": now,
                }
            )
        # 幂等 upsert：重索引同一文档时覆盖旧行（一对一，无残留）
        db.execute(
            text(
                "INSERT INTO pg_chunk_vectors "
                "(chunk_id, embedding, provider, model_name, dimension, "
                "content_sha256, created_at, updated_at) "
                "VALUES (:chunk_id, :embedding, :provider, :model_name, "
                ":dimension, :content_sha256, :created_at, :updated_at) "
                "ON CONFLICT (chunk_id) DO UPDATE SET "
                "embedding = EXCLUDED.embedding, "
                "provider = EXCLUDED.provider, "
                "model_name = EXCLUDED.model_name, "
                "dimension = EXCLUDED.dimension, "
                "content_sha256 = EXCLUDED.content_sha256, "
                "updated_at = EXCLUDED.updated_at"
            ),
            params,
        )

    def delete_vector_rows(self, db: Session, document_id: int) -> None:
        db.execute(
            text(
                "DELETE FROM pg_chunk_vectors WHERE chunk_id IN ("
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
                f"DELETE FROM pg_chunk_vectors WHERE chunk_id IN ({placeholders})"
            ),
            params,
        )

    def orphaned_vector_chunk_ids(self, db: Session) -> list[int]:
        rows = db.execute(
            text(
                "SELECT pv.chunk_id FROM pg_chunk_vectors pv "
                "LEFT JOIN document_chunks dc ON dc.id = pv.chunk_id "
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
                "SELECT 1 FROM pg_chunk_vectors "
                "WHERE provider != :p OR model_name != :m OR dimension != :d LIMIT 1"
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
        # 边界检查：缺 extension/表/维度不匹配 → 稳定 503，绝无静默回退
        raise_for_vector_readiness(db, len(query_vector))
        sql, params = build_vector_sql(document_ids, limit, identity)
        params["q"] = query_vector
        rows = db.execute(text(sql), params).mappings().all()
        candidates = [
            {
                "chunk_id": row["chunk_id"],
                "document_id": row["document_id"],
                "score": float(row["score"]),
            }
            for row in rows
        ]
        total = int(rows[0]["total"]) if rows else 0
        return candidates, total

    def count_vector_rows(
        self, db: Session, document_ids: list[int]
    ) -> dict[int, int]:
        if not document_ids:
            return {}
        placeholders = ", ".join(f":doc_{i}" for i in range(len(document_ids)))
        params = {f"doc_{i}": doc_id for i, doc_id in enumerate(document_ids)}
        rows = db.execute(
            text(
                "SELECT dc.document_id, COUNT(pv.chunk_id) AS n "
                "FROM pg_chunk_vectors pv "
                "JOIN document_chunks dc ON dc.id = pv.chunk_id "
                f"WHERE dc.document_id IN ({placeholders}) "
                "GROUP BY dc.document_id"
            ),
            params,
        ).mappings().all()
        return {row["document_id"]: row["n"] for row in rows}
