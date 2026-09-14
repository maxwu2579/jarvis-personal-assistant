"""SearchBackend 抽象（Phase 5A + 5B）：关键词与向量的方言接缝。

设计（对应 Phase 5A「搜索与 RAG 行为兼容」+ 5B「PostgreSQL 原生向量检索」）：
- SQLite 实现：keyword = FTS5 + BM25（行为不变）；
  vector = chunk_embeddings.vector_json + Python cosine（零依赖路径）；
- PostgreSQL 实现：keyword = 兼容性关键词回退（参数绑定 ILIKE，非 PG FTS）；
  vector = pgvector 原生向量表 + 数据库端 cosine Top-K（HNSW 索引）；
- 两实现返回相同候选结构 [{"chunk_id", "document_id", "score"}]，
  下游（retrieval_service / RAG / 前端）完全不用感知方言；
- 所有用户输入（query 文本、document_ids）只出现在绑定参数中，
  绝不拼接进 SQL 文本；两方言 vector 排序均稳定
  （similarity DESC, document_id ASC, chunk_index ASC, chunk_id ASC）。
"""

from abc import ABC, abstractmethod

from sqlalchemy.orm import Session


class SearchBackend(ABC):
    """检索后端接口：关键词与向量索引写入/查询（方言接缝的唯一事实来源）。"""

    # ---- 关键词（Phase 4B/5A）----

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """稳定标识（日志/README 可见）：'fts5' 或 'ilike-fallback'。"""

    @abstractmethod
    def ensure_keyword_index(self, db: Session) -> None:
        """确保关键词索引存在（幂等）。PG 回退无独立索引表，为无操作。"""

    @abstractmethod
    def write_keyword_rows(
        self, db: Session, document_id: int, rows: list[tuple[int, str]]
    ) -> None:
        """写入/整体重写某文档的关键词行。rows: [(chunk_id, content), ...]。"""

    @abstractmethod
    def delete_keyword_rows(self, db: Session, document_id: int) -> None:
        """删除某文档的关键词行（幂等）。"""

    @abstractmethod
    def keyword_search(
        self,
        db: Session,
        *,
        query: str,
        document_ids: list[int] | None,
        limit: int,
    ) -> list[dict]:
        """关键词查询：返回 [{"chunk_id", "document_id", "score"}]，score 越大越相关。

        安全要求：用户输入永不拼接进 SQL（全部参数绑定）；
        返回顺序稳定（同分按 document_id / chunk_index 确定）。
        """

    # ---- 向量（Phase 5B）----

    @property
    def vector_backend_name(self) -> str:
        """向量路径稳定标识：'python-cosine'（SQLite）或 'pgvector-cosine'（PG）。

        供日志/README 诚实标注；不代表语义质量。
        """
        return "python-cosine"

    def existing_vector_rows(
        self, db: Session, document_id: int
    ) -> dict[int, dict]:
        """某文档现有向量行：chunk_id -> {provider, model, dimension, content_sha256}。

        索引服务用它判断「内容/provider/model/dimension 未变 → 跳过」。
        """
        raise NotImplementedError

    def write_vector_rows(
        self,
        db: Session,
        document_id: int,
        rows: list[tuple[int, list[float], str, str, int, str]],
    ) -> None:
        """写入/覆盖某文档的向量行。

        rows: [(chunk_id, vector, provider, model_name, dimension,
                content_sha256), ...]；按 chunk_id 幂等 upsert。
        SQLite 写 vector_json；PG 写原生 vector 列（零向量显式拒绝）。
        """
        raise NotImplementedError

    def delete_vector_rows(self, db: Session, document_id: int) -> None:
        """删除某文档的向量行（幂等；显式路径，CASCADE 之外的双保险）。"""
        raise NotImplementedError

    def delete_vector_rows_by_chunk_ids(
        self, db: Session, chunk_ids: list[int]
    ) -> None:
        """按 chunk_id 精确删除向量行（幂等；Phase 5C 孤儿清理）。

        孤儿 = 向量行存在但其 chunk 已不存在于 document_chunks
        （SQLite FK pragma 未启用 / 重摄 chunk 集合收缩等历史残留）。
        此方法只做方言无关的批量删除；检测见 orphaned_vector_chunk_ids。
        """
        raise NotImplementedError

    def orphaned_vector_chunk_ids(self, db: Session) -> list[int]:
        """全局孤儿向量行 chunk_id 列表（LEFT JOIN document_chunks 检测）。

        返回 [] 表示无孤儿（正常路径：FK CASCADE 已自动清理）。
        """
        raise NotImplementedError

    def any_stale_vector_rows(
        self,
        db: Session,
        provider: str,
        model: str,
        dimension: int,
    ) -> bool:
        """是否存在身份与当前 provider 不一致的向量行（Phase 5C health）。

        用于 embedding_index_stale 判定：有行且身份过期 → True；
        空表返回 False（无索引可 stale）。
        """
        raise NotImplementedError

    def vector_search(
        self,
        db: Session,
        *,
        query_vector: list[float],
        document_ids: list[int] | None,
        limit: int,
        identity: tuple[str, str, int] | None = None,
    ) -> tuple[list[dict], int]:
        """向量 Top-K 查询：返回 (候选列表, 候选总数)。

        - 候选： [{"chunk_id", "document_id", "score"}]，score = cosine
          similarity（越大越相关），已按稳定顺序截断到 limit；
        - 候选总数 = 参与扫描的全部合法候选数（LIMIT 之前），
          两方言语义一致（SQLite=全扫描数；PG=COUNT(*) OVER()）；
        - identity=(provider_name, model_name, dimension)：Phase 5C 身份
          过滤——只检索当前有效身份的向量行，绝不混入 stale
          （provider/model/dimension 任一过期的旧行）；None = 不过滤
          （内部/兼容路径，生产搜索必须传身份）；
        - SQLite 超 candidate_limit → RetrievalCandidateLimitExceededError；
        - PG 缺 extension/表/维度不匹配 → 稳定 RetrievalError（503）。
        """
        raise NotImplementedError

    def count_vector_rows(
        self, db: Session, document_ids: list[int]
    ) -> dict[int, int]:
        """每个文档的已索引向量数（列表接口计数，避免 N+1 查询）。"""
        raise NotImplementedError
