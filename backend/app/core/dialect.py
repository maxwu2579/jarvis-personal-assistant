"""数据库方言识别与能力抽象（Phase 5A）。

单一事实来源：所有「SQLite vs PostgreSQL」的判断必须经过本模块，
业务服务禁止散落 `if sqlite` / `if postgres` 分支（见 README 双数据库架构）。
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class DatabaseDialect(str, Enum):
    SQLITE = "sqlite"
    POSTGRESQL = "postgresql"


def detect_dialect(database_url: str) -> DatabaseDialect:
    """从 SQLAlchemy URL 前缀识别方言。

    覆盖 sqlite://、sqlite+pysqlite://（及其他 sqlite+ 变体）、
    postgresql://、postgres://、postgresql+psycopg:// 等常见写法。
    未知方言响亮失败——绝不静默假设为 SQLite。
    """
    prefix = (database_url or "").split("://", 1)[0].strip().lower()
    if not prefix:
        raise ValueError("database_url is empty; cannot detect dialect")
    if prefix == "sqlite" or prefix.startswith("sqlite+"):
        return DatabaseDialect.SQLITE
    if prefix in ("postgresql", "postgres") or prefix.startswith(
        ("postgresql+", "postgres+")
    ):
        return DatabaseDialect.POSTGRESQL
    raise ValueError(f"unsupported database dialect: {prefix!r}")


@dataclass(frozen=True)
class DatabaseCapabilities:
    """方言能力清单（服务层按能力分支，而不是按名字猜）。

    - fts5：SQLite 分支持有 FTS5 虚拟表 + BM25；
    - partial_index：部分唯一索引（SQLite 与 PG 均支持，方言参数名不同）；
    - ilike_fallback：PG 分支的兼容关键词回退（ILIKE，非 pgvector/PG FTS）。
    """

    dialect: DatabaseDialect
    fts5: bool
    partial_index: bool
    ilike_fallback: bool

    @classmethod
    def for_dialect(cls, dialect: DatabaseDialect) -> "DatabaseCapabilities":
        if dialect is DatabaseDialect.SQLITE:
            return cls(
                dialect=dialect,
                fts5=True,
                partial_index=True,
                ilike_fallback=False,
            )
        return cls(
            dialect=dialect,
            fts5=False,
            partial_index=True,
            ilike_fallback=True,
        )


def coerce_utc_for_db(value: datetime, dialect: DatabaseDialect) -> datetime:
    """写入数据库前的 UTC 归一化（方言差异的唯一时区接缝）。

    - SQLite：既有约定存 naive UTC（SQLAlchemy 的 SQLite 方言不保留时区）；
    - PostgreSQL：DateTime(timezone=True) → TIMESTAMPTZ，必须存 aware UTC，
      否则 naive 参数与列比较会按会话时区解释（产生偏移错误）。
    输出侧统一由 schemas.common.serialize_utc 序列化，两方言最终格式一致。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    if dialect is DatabaseDialect.SQLITE:
        return value.replace(tzinfo=None)
    return value
