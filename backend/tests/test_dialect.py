"""Phase 5A 方言抽象单元测试：方言识别、能力清单、UTC 归一化。

对应规范要求：方言识别（22 项之 2）、UTC 不变（之 14）。
纯函数测试，不依赖任何数据库连接。
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.core.dialect import (
    DatabaseCapabilities,
    DatabaseDialect,
    coerce_utc_for_db,
    detect_dialect,
)


class TestDetectDialect:
    @pytest.mark.parametrize(
        "url",
        [
            "sqlite:///./jarvis.db",
            "sqlite:////absolute/path.db",
            "sqlite+pysqlite:///./jarvis.db",
            "sqlite+aiosqlite:///:memory:",
            "SQLITE:///./UPPER.db",
        ],
    )
    def test_sqlite_variants(self, url):
        assert detect_dialect(url) is DatabaseDialect.SQLITE

    @pytest.mark.parametrize(
        "url",
        [
            "postgresql://user:pass@localhost:5432/db",
            "postgres://user:pass@localhost:5432/db",
            "postgresql+psycopg://user:pass@localhost:5432/db",
            "postgres+psycopg://user:pass@localhost:5432/db",
            "POSTGRESQL://user:pass@localhost:5432/db",
        ],
    )
    def test_postgresql_variants(self, url):
        assert detect_dialect(url) is DatabaseDialect.POSTGRESQL

    def test_password_never_required_for_detection(self):
        # 方言识别只看前缀，不解析/不输出任何连接细节
        assert (
            detect_dialect("postgresql://u:p4ssw0rd_secret_x@h:5432/db")
            is DatabaseDialect.POSTGRESQL
        )

    @pytest.mark.parametrize("url", ["", None])
    def test_empty_url_raises(self, url):
        with pytest.raises(ValueError):
            detect_dialect(url)

    @pytest.mark.parametrize("url", ["mysql://u@h/db", "oracle://u@h/db", "://u@h/db"])
    def test_unknown_dialect_raises(self, url):
        # 未知方言响亮失败——绝不静默假设为 SQLite
        with pytest.raises(ValueError):
            detect_dialect(url)


class TestDatabaseCapabilities:
    def test_sqlite_capabilities(self):
        caps = DatabaseCapabilities.for_dialect(DatabaseDialect.SQLITE)
        assert caps.fts5 is True
        assert caps.partial_index is True
        assert caps.ilike_fallback is False

    def test_postgresql_capabilities(self):
        caps = DatabaseCapabilities.for_dialect(DatabaseDialect.POSTGRESQL)
        assert caps.fts5 is False  # PG 分支绝不建 FTS5
        assert caps.partial_index is True  # 两方言都支持 partial unique index
        assert caps.ilike_fallback is True  # 诚实标注的兼容回退，非 pgvector

    def test_capabilities_are_frozen(self):
        caps = DatabaseCapabilities.for_dialect(DatabaseDialect.SQLITE)
        with pytest.raises(Exception):
            caps.fts5 = False


class TestCoerceUtcForDb:
    def test_naive_interpreted_as_utc_for_postgresql(self):
        naive = datetime(2026, 8, 11, 4, 0)
        out = coerce_utc_for_db(naive, DatabaseDialect.POSTGRESQL)
        assert out.tzinfo is not None
        assert out.utcoffset() == timedelta(0)
        assert out == datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)

    def test_aware_other_tz_converted_to_utc_for_postgresql(self):
        shanghai = datetime(2026, 8, 11, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        out = coerce_utc_for_db(shanghai, DatabaseDialect.POSTGRESQL)
        assert out.utcoffset() == timedelta(0)
        assert out.hour == 4  # 12:00+08:00 == 04:00 UTC

    def test_sqlite_keeps_historical_naive_utc_convention(self):
        aware = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
        out = coerce_utc_for_db(aware, DatabaseDialect.SQLITE)
        assert out.tzinfo is None
        assert out == datetime(2026, 8, 11, 4, 0)

    def test_sqlite_strips_other_tz_to_naive_utc(self):
        shanghai = datetime(2026, 8, 11, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        out = coerce_utc_for_db(shanghai, DatabaseDialect.SQLITE)
        assert out.tzinfo is None
        assert out == datetime(2026, 8, 11, 4, 0)

    def test_microseconds_preserved(self):
        aware = datetime(2026, 8, 11, 4, 0, 0, 123456, tzinfo=timezone.utc)
        for dialect in (DatabaseDialect.SQLITE, DatabaseDialect.POSTGRESQL):
            out = coerce_utc_for_db(aware, dialect)
            assert out.microsecond == 123456
