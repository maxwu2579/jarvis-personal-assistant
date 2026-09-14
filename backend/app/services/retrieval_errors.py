"""检索领域错误（Phase 4B + 5B）。

稳定错误码 → HTTP 状态码映射（main.py 全局 handler 使用）：
- DOCUMENT_NOT_READY            409   索引/重索引非 READY 文档
- DOCUMENT_NOT_INDEXED          409   预留：对未索引文档的显式操作（4C 引用用）
- RETRIEVAL_NOT_AVAILABLE       503   底层检索能力不可用（如 SQLite 无 FTS5）
- INVALID_SEARCH_QUERY          422   空白/超长 query（schema 校验的兜底）
- INVALID_SEARCH_MODE           422   未知检索模式（预留：schema 枚举已拦截）
- EMBEDDING_DIMENSION_MISMATCH  422   向量维度与 provider 不一致（预留）
- RETRIEVAL_CANDIDATE_LIMIT_EXCEEDED  422  向量候选超上限（提示未来 pgvector）
- INDEXING_FAILED               500   索引阶段失败（事务已回滚，状态置 INDEX_FAILED）
- VECTOR_EXTENSION_MISSING      503   pgvector extension 未安装（环境能力问题）
- VECTOR_SCHEMA_MISSING         503   pg_chunk_vectors 表缺失（PG 需先跑 Alembic）
- VECTOR_DIMENSION_MISMATCH     503   表列维度与配置/provider 维度不一致
  （已建 VECTOR(384) 后仅改环境变量 → 稳定报错，绝不截断/填充/转换）
- MIGRATION_NOT_CURRENT         503   数据库迁移版本不是当前 head（PG readiness）

文案全部 sanitized：不含路径、SQL、Traceback、向量内容、API Key。
"""

RETRIEVAL_ERROR_STATUS: dict[str, int] = {
    "DOCUMENT_NOT_READY": 409,
    "DOCUMENT_NOT_INDEXED": 409,
    "RETRIEVAL_NOT_AVAILABLE": 503,
    "INVALID_SEARCH_QUERY": 422,
    "INVALID_SEARCH_MODE": 422,
    "EMBEDDING_DIMENSION_MISMATCH": 422,
    "RETRIEVAL_CANDIDATE_LIMIT_EXCEEDED": 422,
    "INDEXING_FAILED": 500,
    "DOCUMENT_NOT_FOUND": 404,
    "VECTOR_EXTENSION_MISSING": 503,
    "VECTOR_SCHEMA_MISSING": 503,
    "VECTOR_DIMENSION_MISMATCH": 503,
    "MIGRATION_NOT_CURRENT": 503,
}


class RetrievalError(Exception):
    """检索领域错误基类。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class DocumentNotReadyError(RetrievalError):
    def __init__(self, document_id: int, actual: str):
        super().__init__(
            "DOCUMENT_NOT_READY",
            f"Document {document_id} is {actual}, only READY documents can be indexed",
        )


class DocumentNotIndexedError(RetrievalError):
    def __init__(self, document_id: int):
        super().__init__(
            "DOCUMENT_NOT_INDEXED", f"Document {document_id} has no retrieval index"
        )


class RetrievalNotAvailableError(RetrievalError):
    def __init__(self, message: str = "full-text search (FTS5) is not available"):
        super().__init__("RETRIEVAL_NOT_AVAILABLE", message)


class InvalidSearchQueryError(RetrievalError):
    def __init__(self, message: str):
        super().__init__("INVALID_SEARCH_QUERY", message)


class InvalidSearchModeError(RetrievalError):
    def __init__(self, mode: str):
        super().__init__("INVALID_SEARCH_MODE", f"unknown search mode {mode!r}")


class EmbeddingDimensionMismatchError(RetrievalError):
    def __init__(self, expected: int, actual: int):
        super().__init__(
            "EMBEDDING_DIMENSION_MISMATCH",
            f"embedding dimension mismatch (expected {expected}, got {actual})",
        )


class VectorDimensionMismatchError(RetrievalError):
    """PG schema 列维度与配置/provider 维度不一致（Phase 5B）。

    已建立 VECTOR(384) 后仅改 EMBEDDING_DIMENSION 环境变量 → 稳定 503，
    绝不自动截断、填充或转换向量；修复方式是显式 schema 迁移 + reindex。
    文案不包含维度数值以外的细节（脱敏，无 SQL/堆栈）。
    """

    def __init__(self, expected: int, actual: int | None):
        super().__init__(
            "VECTOR_DIMENSION_MISMATCH",
            "vector dimension mismatch between database schema and configuration; "
            "do not change EMBEDDING_DIMENSION without a schema migration "
            f"(expected {expected}, schema {actual})",
        )


class RetrievalCandidateLimitExceededError(RetrievalError):
    def __init__(self, total: int, limit: int):
        super().__init__(
            "RETRIEVAL_CANDIDATE_LIMIT_EXCEEDED",
            f"too many vector candidates ({total} > {limit}); "
            "this lightweight scanner targets small datasets, "
            "migrate to PostgreSQL + pgvector for production scale",
        )


class IndexingFailedError(RetrievalError):
    def __init__(self, document_id: int, message: str):
        super().__init__("INDEXING_FAILED", f"indexing document {document_id} failed")
        self.document_id = document_id
        self.reason = message
