"""检索服务（Phase 4B）：keyword / vector / hybrid 三种模式。

统一输入：query、document_ids（可选）、top_k（1-20）、mode（默认 hybrid）。
统一输出结构（为 Phase 4C citations 提供真实 chunk 定位信息）：
    {"query", "mode", "items": [{document_id, document_title, chunk_id,
      chunk_index, content(预览), content_length, char_start, char_end,
      keyword_score, vector_score, final_score}],
     "total_candidates", "embedding_model"}

分数语义：final_score 越大越相关（BM25 方向已统一为 -bm25）。

- keyword：关键词检索（SQLite=FTS5 MATCH + bm25；PG=兼容 ILIKE 回退，
  见 SearchBackend）+ document_ids 过滤；只含已成功索引（从而 READY）的 chunk；
- vector：query 经 EmbeddingProvider 生成向量 → SearchBackend 向量检索
  （SQLite=vector_json + Python cosine，PG=pgvector 数据库端 Top-K，
  接口与候选结构跨方言一致，见 search/base.py）；
  Phase 5C 身份过滤：只检索当前有效身份（provider/model/dimension）
  的向量行，身份过期（未 reindex）的旧行绝不混入检索结果；
  零向量 query → 直接返回空结果（不生成无意义相似度）；
  非法 vector_json / 维度不符（SQLite 路径）→ 跳过并记录日志，绝不崩溃；
  SQLite 候选数超 retrieval_candidate_limit → 明确错误（提示未来 pgvector）；
- hybrid：Reciprocal Rank Fusion（RRF）融合两个列表——
  final = keyword_weight/(k+rank_kw) + vector_weight/(k+rank_vec)；
  两分数量纲不同（BM25 与 cosine），绝不直接相加；同一 chunk 合并不重复；
  单侧无结果时另一侧仍可返回。

诚实命名：LocalHashEmbedding 不是神经语义模型，README/UI 只称
「轻量词汇向量检索 / lightweight lexical-vector retrieval」；
只有显式配置的 OpenAI provider 产生的结果才算语义 embedding。
"""

import logging
from dataclasses import dataclass, replace

from sqlalchemy.orm import Session

from app.core.config import settings
from app.embeddings.base import EmbeddingProvider
from app.embeddings.factory import create_embedding_provider
from app.models.document import Document, DocumentChunk
from app.schemas.document import CHUNK_PREVIEW_LIMIT
from app.search.factory import get_search_backend
from app.services.retrieval_errors import (
    InvalidSearchModeError,
    InvalidSearchQueryError,
)

logger = logging.getLogger(__name__)

# 检索模式（与 schema 枚举一致的唯一事实来源，避免重复定义漂移）
KEYWORD = "keyword"
VECTOR = "vector"
HYBRID = "hybrid"
SEARCH_MODES = frozenset({KEYWORD, VECTOR, HYBRID})

MAX_QUERY_LENGTH = 1000
MAX_TOP_K = 20


@dataclass
class SearchItem:
    document_id: int
    document_title: str
    chunk_id: int
    chunk_index: int
    content: str
    content_length: int
    char_start: int
    char_end: int
    keyword_score: float
    vector_score: float
    final_score: float


@dataclass
class SearchResult:
    query: str
    mode: str
    items: list[SearchItem]
    total_candidates: int
    embedding_model: str


def _load_chunk_details(db: Session, chunk_ids: list[int]) -> dict[int, dict]:
    """加载 chunk 定位信息与内容（预览截断）。"""
    if not chunk_ids:
        return {}
    rows = (
        db.query(DocumentChunk, Document)
        .join(Document, Document.id == DocumentChunk.document_id)
        .filter(DocumentChunk.id.in_(chunk_ids))
        .all()
    )
    details: dict[int, dict] = {}
    for chunk, document in rows:
        details[chunk.id] = {
            "chunk_id": chunk.id,
            "document_id": document.id,
            "document_title": document.original_filename,
            "chunk_index": chunk.chunk_index,
            "content": chunk.content[:CHUNK_PREVIEW_LIMIT]
            + ("…" if len(chunk.content) > CHUNK_PREVIEW_LIMIT else ""),
            "content_length": len(chunk.content),
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
        }
    return details


class RetrievalService:
    def __init__(self, db: Session, provider: EmbeddingProvider | None = None):
        self.db = db
        self.provider = provider or create_embedding_provider()
        self.backend = get_search_backend()

    def search(
        self,
        *,
        query: str,
        mode: str = HYBRID,
        top_k: int = 5,
        document_ids: list[int] | None = None,
    ) -> SearchResult:
        self._validate_input(query, mode, top_k, document_ids)

        if mode == KEYWORD:
            items = self._search_keyword(query, top_k, document_ids)
            total = len(items)
        elif mode == VECTOR:
            items, total = self._search_vector(query, top_k, document_ids)
        else:  # hybrid
            items, total = self._search_hybrid(query, top_k, document_ids)

        return SearchResult(
            query=query,
            mode=mode,
            items=items,
            total_candidates=total,
            embedding_model=self.provider.model_name,
        )

    # ---- 校验 ----

    @staticmethod
    def _validate_input(
        query: str, mode: str, top_k: int, document_ids: list[int] | None
    ) -> None:
        stripped = (query or "").strip()
        if not stripped:
            raise InvalidSearchQueryError("query must not be blank")
        if len(stripped) > MAX_QUERY_LENGTH:
            raise InvalidSearchQueryError(
                f"query too long ({len(stripped)} chars > {MAX_QUERY_LENGTH})"
            )
        if mode not in SEARCH_MODES:
            raise InvalidSearchModeError(mode)
        if not 1 <= top_k <= MAX_TOP_K:
            raise InvalidSearchQueryError(
                f"top_k must be between 1 and {MAX_TOP_K} (got {top_k})"
            )
        if document_ids:
            unique = list(dict.fromkeys(document_ids))
            if len(unique) != len(document_ids):
                raise InvalidSearchQueryError("document_ids must not contain duplicates")

    # ---- keyword ----

    def _search_keyword(
        self, query: str, top_k: int, document_ids: list[int] | None
    ) -> list[SearchItem]:
        candidates = self.backend.keyword_search(
            self.db,
            query=query,
            document_ids=document_ids,
            limit=top_k,
        )
        details = _load_chunk_details(
            self.db, [c["chunk_id"] for c in candidates]
        )
        items: list[SearchItem] = []
        for candidate in candidates:
            detail = details.get(candidate["chunk_id"])
            if detail is None:
                continue  # 索引与 chunks 不一致的防御（不崩溃）
            score = float(candidate["score"])
            items.append(
                SearchItem(
                    **detail,
                    keyword_score=score,
                    vector_score=0.0,
                    final_score=score,
                )
            )
        return items

    # ---- vector ----

    def _search_vector(
        self, query: str, top_k: int, document_ids: list[int] | None
    ) -> tuple[list[SearchItem], int]:
        query_vector = self.provider.embed_query(query)
        # 零向量防御：无可搜索 token 的 query 不生成无意义相似度结果
        if not any(value != 0.0 for value in query_vector):
            logger.warning("retrieval.zero_query_vector query_len=%s", len(query))
            return [], 0
        # Phase 5C 身份过滤：只检索当前有效身份的向量行，绝不混入 stale
        # （provider/model/dimension 任一过期的旧行；health 会在索引
        # 未刷新时提示 embedding_index_stale，但搜索本身不降级混入）
        identity = (
            self.provider.provider_name,
            self.provider.model_name,
            self.provider.dimension,
        )
        # 后端负责方言细节：SQLite=vector_json+Python cosine（含候选上限
        # 与非法向量跳过），PG=pgvector 数据库端 Top-K（含就绪检查）
        candidates, total_candidates = self.backend.vector_search(
            self.db,
            query_vector=query_vector,
            document_ids=document_ids,
            limit=top_k,
            identity=identity,
        )
        details = _load_chunk_details(
            self.db, [c["chunk_id"] for c in candidates]
        )
        items: list[SearchItem] = []
        for candidate in candidates:
            detail = details.get(candidate["chunk_id"])
            if detail is None:
                continue  # 索引与 chunks 不一致的防御（不崩溃）
            score = float(candidate["score"])
            items.append(
                SearchItem(
                    **detail,
                    keyword_score=0.0,
                    vector_score=score,
                    final_score=score,
                )
            )
        return items, total_candidates

    # ---- hybrid ----

    def _search_hybrid(
        self, query: str, top_k: int, document_ids: list[int] | None
    ) -> tuple[list[SearchItem], int]:
        keyword_items = self._search_keyword(query, top_k * 10, document_ids)
        vector_items, vector_total = self._search_vector(query, top_k * 10, document_ids)
        total_candidates = len(keyword_items) + vector_total

        # RRF：rank 从 1 开始；同分用稳定次序（keyword 结果按 (score,doc,chunk) 已排好）
        rrf_k = settings.retrieval_rrf_k
        kw_weight = settings.retrieval_keyword_weight
        vec_weight = settings.retrieval_vector_weight

        fused: dict[int, SearchItem] = {}
        for rank, item in enumerate(keyword_items, start=1):
            fused[item.chunk_id] = replace(
                item,
                vector_score=0.0,
                final_score=kw_weight / (rrf_k + rank),
            )
        for rank, item in enumerate(vector_items, start=1):
            existing = fused.get(item.chunk_id)
            if existing is not None:
                existing.vector_score = item.vector_score
                existing.final_score += vec_weight / (rrf_k + rank)
            else:
                fused[item.chunk_id] = replace(
                    item,
                    keyword_score=0.0,
                    final_score=vec_weight / (rrf_k + rank),
                )

        merged = list(fused.values())
        merged.sort(
            key=lambda item: (
                -item.final_score,
                item.document_id,
                item.chunk_index,
            )
        )
        return merged[:top_k], total_candidates
