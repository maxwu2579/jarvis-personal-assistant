"""Embedding 层（Phase 4B）：Provider 抽象 + 零网络 LocalHash 实现 + 工厂。

只负责「文本 → 向量」，不访问数据库、不执行检索。
"""

from app.embeddings.base import (
    EmbeddingBatchTooLargeError,
    EmbeddingError,
    EmbeddingInputError,
    EmbeddingInternalError,
    EmbeddingProvider,
)
from app.embeddings.factory import create_embedding_provider
from app.embeddings.local_hash_provider import LocalHashEmbeddingProvider

__all__ = [
    "EmbeddingBatchTooLargeError",
    "EmbeddingError",
    "EmbeddingInputError",
    "EmbeddingInternalError",
    "EmbeddingProvider",
    "LocalHashEmbeddingProvider",
    "create_embedding_provider",
]
