"""PostgreSQL pgvector readiness 检查（Phase 5B）。

职责：在 PG 模式下回答「数据库向量能力是否就绪」，供
- /health/db readiness（稳定脱敏状态码）
- PG 向量写入/查询边界（稳定 RetrievalError，不返回 SQL/堆栈）

检查顺序（任一失败即短路返回对应稳定码）：
1. DB_UNAVAILABLE            连接/SELECT 失败
2. MIGRATION_NOT_CURRENT     alembic_version 缺失或版本 != 当前 head
3. VECTOR_EXTENSION_MISSING  vector extension 未安装
4. VECTOR_SCHEMA_MISSING     pg_chunk_vectors 表缺失
5. VECTOR_DIMENSION_MISMATCH 列维度与期望维度不一致（绝不截断/填充/转换）

实现说明：本模块只使用普通 SQLAlchemy text() 查询，不 import pgvector 类型，
SQLite 路径可安全导入；PG 专属查询仅在 DIALECT 为 PostgreSQL 时被调用。
"""

import re

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.services.retrieval_errors import RetrievalError, VectorDimensionMismatchError

# 当前 Alembic head revision（Phase 6B 迁移链 head）。
# 与 backend/alembic/versions/ 下的最新迁移保持一致；测试断言二者相等。
EXPECTED_MIGRATION_HEAD = "8c9d0e1f2a3b4"

# 稳定 readiness 状态码（结构化日志与错误响应使用，不泄漏细节）
DB_UNAVAILABLE = "DB_UNAVAILABLE"
MIGRATION_NOT_CURRENT = "MIGRATION_NOT_CURRENT"
VECTOR_EXTENSION_MISSING = "VECTOR_EXTENSION_MISSING"
VECTOR_SCHEMA_MISSING = "VECTOR_SCHEMA_MISSING"
VECTOR_DIMENSION_MISMATCH = "VECTOR_DIMENSION_MISMATCH"

# format_type 对固定维度 vector 列的返回形如 "vector(384)"
_VECTOR_TYPE_RE = re.compile(r"^vector\((\d+)\)$", re.IGNORECASE)

# 稳定脱敏文案（不含 URL/密码/SQL/堆栈）
_MESSAGES: dict[str, str] = {
    DB_UNAVAILABLE: "Database is unavailable",
    MIGRATION_NOT_CURRENT: "Database migrations are not at the current head",
    VECTOR_EXTENSION_MISSING: (
        "pgvector extension is not installed on this PostgreSQL server"
    ),
    VECTOR_SCHEMA_MISSING: "pg_chunk_vectors table is missing (run alembic upgrade head)",
    VECTOR_DIMENSION_MISMATCH: (
        "vector dimension mismatch between database schema and configuration; "
        "do not change EMBEDDING_DIMENSION without a schema migration"
    ),
}


def parse_vector_dimension(type_name: str | None) -> int | None:
    """从 format_type 输出解析固定向量维度：'vector(384)' -> 384。

    无维度/非 vector 类型返回 None（调用方按不匹配处理）。
    """
    if not type_name:
        return None
    match = _VECTOR_TYPE_RE.match(type_name.strip())
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:  # pragma: no cover - 正则已保证数字
        return None


def check_pg_vector_readiness(
    db, expected_dimension: int
) -> str | None:
    """返回 None（就绪）或稳定状态码（见模块 docstring 检查顺序）。

    db 接受任何提供 execute(stmt) 的会话（真实 Session 或测试桩）。
    """
    try:
        version = db.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar()
    except SQLAlchemyError:
        return DB_UNAVAILABLE
    if version != EXPECTED_MIGRATION_HEAD:
        return MIGRATION_NOT_CURRENT

    try:
        extension = db.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
        ).scalar()
    except SQLAlchemyError:
        return DB_UNAVAILABLE
    if not extension:
        return VECTOR_EXTENSION_MISSING

    try:
        table = db.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'pg_chunk_vectors'"
            )
        ).scalar()
    except SQLAlchemyError:
        return DB_UNAVAILABLE
    if not table:
        return VECTOR_SCHEMA_MISSING

    try:
        type_name = db.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) "
                "FROM pg_catalog.pg_attribute a "
                "WHERE a.attrelid = 'pg_chunk_vectors'::regclass "
                "AND a.attname = 'embedding'"
            )
        ).scalar()
    except SQLAlchemyError:
        return DB_UNAVAILABLE
    actual = parse_vector_dimension(type_name)
    if actual != expected_dimension:
        return VECTOR_DIMENSION_MISMATCH
    return None


def readiness_message(code: str) -> str:
    """稳定脱敏文案（/health/db 响应使用，不含 URL/密码/SQL/堆栈）。"""
    return _MESSAGES[code]


def raise_for_vector_readiness(db, expected_dimension: int) -> None:
    """PG 向量写入/查询边界的严格检查：不满足 → 稳定 RetrievalError。

    VECTOR_DIMENSION_MISMATCH 使用已有 EmbeddingDimensionMismatchError 的
    稳定码（expected/actual 仅供日志，响应文案脱敏）。
    """
    code = check_pg_vector_readiness(db, expected_dimension)
    if code is None:
        return
    if code == VECTOR_DIMENSION_MISMATCH:
        # 实际维度从列类型再解析一次（仅用于错误消息；缺失按 None 处理）
        actual: int | None = None
        try:
            type_name = db.execute(
                text(
                    "SELECT format_type(a.atttypid, a.atttypmod) "
                    "FROM pg_catalog.pg_attribute a "
                    "WHERE a.attrelid = 'pg_chunk_vectors'::regclass "
                    "AND a.attname = 'embedding'"
                )
            ).scalar()
            actual = parse_vector_dimension(type_name)
        except SQLAlchemyError:  # pragma: no cover - 已通过检查路径
            actual = None
        raise VectorDimensionMismatchError(expected_dimension, actual)
    raise RetrievalError(code, _MESSAGES[code])
