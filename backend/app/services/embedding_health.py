"""Embedding 生产就绪健康检查（Phase 5C）。

/health/embedding 的单一事实来源。检查顺序（任一命中即短路返回）：

1. provider_key_missing       provider=openai 但 EMBEDDING_API_KEY 未注入
                               （Key 只能从环境注入，配置缺 Key 即失败）
2. provider_config_invalid    provider 构造失败（未知模型 / 维度不合法 /
                               配置非法），携带稳定错误码与脱敏消息
3. vector_extension_missing   （仅 PG 方言）pgvector extension 未安装
4. vector_schema_missing      （仅 PG 方言）pg_chunk_vectors 表缺失
5. vector_dimension_mismatch  （仅 PG 方言）列维度 != provider 维度
                               （绝不截断/填充/转换）
6. embedding_index_stale      已有向量行的身份（provider/model/dimension）
                               与当前 provider 不一致 → 需要显式 reindex
7. ready                      全部通过

诚实边界：
- verified 恒为 False：本检查不做在线 smoke（不自动消费 API 额度），
  真实语义验证由 RUN_LIVE_EMBEDDING_TESTS 门控的集成测试完成；
  「未在线 smoke 不得 verified=true」。
- SQLite 方言跳过 extension/schema/dimension 三项（不适用）；
- DB 不可达（SQLAlchemyError）→ database_unavailable（诚实环境态，
  与 /health/db 的 DB_UNAVAILABLE 同义，不伪装成配置错误）；
- 响应不含任何 Key / URL / 向量 / 原文。
"""

import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.embeddings.base import EmbeddingError
from app.embeddings.factory import create_embedding_provider
from app.search.factory import get_search_backend
from app.services.vector_readiness import (
    DB_UNAVAILABLE,
    MIGRATION_NOT_CURRENT,
    VECTOR_DIMENSION_MISMATCH,
    VECTOR_EXTENSION_MISSING,
    VECTOR_SCHEMA_MISSING,
    check_pg_vector_readiness,
)

logger = logging.getLogger(__name__)

# 规范 7 态（文档/前端可见）。注意：以 HEALTH_ 前缀区分上游
# vector_readiness 的大写稳定码（VECTOR_EXTENSION_MISSING 等），
# 避免同名常量互相覆盖。
READY = "ready"
PROVIDER_CONFIG_INVALID = "provider_config_invalid"
PROVIDER_KEY_MISSING = "provider_key_missing"
HEALTH_VECTOR_EXTENSION_MISSING = "vector_extension_missing"
HEALTH_VECTOR_SCHEMA_MISSING = "vector_schema_missing"
HEALTH_VECTOR_DIMENSION_MISMATCH = "vector_dimension_mismatch"
EMBEDDING_INDEX_STALE = "embedding_index_stale"
# 诚实环境扩展态（DB 层问题归 /health/db；此处如实上报不硬凑 7 态）
DATABASE_UNAVAILABLE = "database_unavailable"

# 上游 PG 就绪稳定码 → health 态映射（DB 不可达/迁移问题统一归环境态）
_PG_CODE_TO_STATUS = {
    VECTOR_EXTENSION_MISSING: HEALTH_VECTOR_EXTENSION_MISSING,
    VECTOR_SCHEMA_MISSING: HEALTH_VECTOR_SCHEMA_MISSING,
    VECTOR_DIMENSION_MISMATCH: HEALTH_VECTOR_DIMENSION_MISMATCH,
}

_MESSAGES: dict[str, str] = {
    READY: "embedding provider is ready",
    PROVIDER_CONFIG_INVALID: "embedding provider configuration is invalid",
    PROVIDER_KEY_MISSING: (
        "embedding provider requires an API key; inject EMBEDDING_API_KEY "
        "via the environment (never in code or config files)"
    ),
    HEALTH_VECTOR_EXTENSION_MISSING: (
        "pgvector extension is not installed on this PostgreSQL server"
    ),
    HEALTH_VECTOR_SCHEMA_MISSING: (
        "pg_chunk_vectors table is missing (run alembic upgrade head)"
    ),
    HEALTH_VECTOR_DIMENSION_MISMATCH: (
        "vector dimension mismatch between database schema and the active "
        "embedding provider; do not change EMBEDDING_DIMENSION without a "
        "schema migration"
    ),
    EMBEDDING_INDEX_STALE: (
        "existing vector rows were written by a different embedding "
        "provider/model/dimension; reindex explicitly to refresh"
    ),
    DATABASE_UNAVAILABLE: "Database is unavailable",
}


def check_embedding_health(db) -> dict:
    """7 态检查（见模块 docstring）。db 提供 execute() 即可（真实或测试桩）。"""
    from app.core.config import settings

    # 1-2. provider 配置/构造检查（fail-closed：构造失败即非 ready）
    cfg_provider = settings.embedding_provider
    if cfg_provider == "openai" and not settings.embedding_api_key.strip():
        return _report(PROVIDER_KEY_MISSING)
    try:
        provider = create_embedding_provider()
    except EmbeddingError as exc:
        logger.warning("embedding.health_provider_invalid code=%s", exc.code)
        return _report(
            PROVIDER_CONFIG_INVALID,
            provider_code=exc.code,
            message=exc.message,
        )

    # 3-5. PG 方言：extension / schema / dimension（SQLite 不适用）
    dialect = getattr(db, "bind", None)
    dialect_name = (
        getattr(dialect, "dialect", None).name
        if dialect is not None
        else None
    )
    if dialect_name is None:
        # 测试桩兼容：从 URL 判定（无 bind 时按非 PG 处理）
        url = getattr(db, "url", None)
        dialect_name = getattr(url, "drivername", "sqlite").split("+")[0]
    if dialect_name == "postgresql":
        code = _check_pg(db, provider.dimension)
        if code is not None:
            return _report(code)

    # 6. stale：已有向量行身份与当前 provider 不一致 → 需要 reindex
    backend = get_search_backend()
    try:
        stale = backend.any_stale_vector_rows(
            db, provider.provider_name, provider.model_name, provider.dimension
        )
    except SQLAlchemyError:
        return _report(DATABASE_UNAVAILABLE)
    if stale:
        return _report(EMBEDDING_INDEX_STALE)

    # 7. ready
    caps = provider.capabilities()
    return _report(
        READY,
        provider=provider.provider_name,
        model=provider.model_name,
        dimension=provider.dimension,
        semantic=bool(caps.get("semantic")),
        verified=False,  # 未在线 smoke 不得 verified=true
    )


def _check_pg(db, expected_dimension: int) -> str | None:
    """PG 就绪码 → health 态；DB 不可达/迁移问题 → 环境态。"""
    code = check_pg_vector_readiness(db, expected_dimension)
    if code is None:
        return None
    if code in _PG_CODE_TO_STATUS:
        return _PG_CODE_TO_STATUS[code]
    if code in (DB_UNAVAILABLE, MIGRATION_NOT_CURRENT):
        return DATABASE_UNAVAILABLE
    return PROVIDER_CONFIG_INVALID  # pragma: no cover - 防御


def _report(
    status: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    dimension: int | None = None,
    semantic: bool | None = None,
    verified: bool | None = None,
    provider_code: str | None = None,
    message: str | None = None,
) -> dict:
    payload: dict = {
        "status": status,
        "verified": bool(verified),
    }
    if provider is not None:
        payload["provider"] = provider
    if model is not None:
        payload["model"] = model
    if dimension is not None:
        payload["dimension"] = dimension
    if semantic is not None:
        payload["semantic"] = semantic
    if provider_code is not None:
        payload["error_code"] = provider_code
    payload["message"] = message or _MESSAGES[status]
    return payload
