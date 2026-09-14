"""检索后端包（Phase 5A）：按数据库方言装配关键词检索实现。

SQLite → FTS5（既有行为）；PostgreSQL → 兼容 ILIKE 回退（诚实标注，
非 pgvector / 非 PG FTS；5B 将替换为真实语义 Embedding + pgvector）。
"""
