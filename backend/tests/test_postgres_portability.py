"""PostgreSQL 可移植性测试（Phase 5A，离线/编译级，无需真实 PG 服务器）。

- 所有模型在 PG dialect 下可编译（DDL），且不含 FTS5 虚拟表；
- partial unique index 在两方言下都带 WHERE（语义不变）；
- 迁移链 PG 离线编译（--sql）：无 batch 错误、无 CREATE VIRTUAL TABLE、
  URL 密码不泄漏进 SQL 输出；
- 健康检查拆分：/health 存活（不碰 DB）、/health/db readiness；
- 数据库不可用 → 稳定脱敏 503（绝不泄露连接串/密码/SQL/堆栈）。
"""

import contextlib
import io
from pathlib import Path

import pytest
from sqlalchemy import DateTime, create_engine, event, inspect, text
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateIndex, CreateTable

from alembic import command
from alembic.config import Config

import app.models  # noqa: F401  注册全部模型
from app.core.database import Base, get_db
from app.main import app

BACKEND_DIR = Path(__file__).resolve().parents[1]

# 假密码：若出现在任何离线 SQL / 错误响应中即为泄漏（测试失败）
_FAKE_PASSWORD = "p4ssw0rd_secret_x"
_PG_TEST_URL = (
    f"postgresql+psycopg://jarvis:{_FAKE_PASSWORD}@localhost:5432/jarvis_test"
)


def _pg_dialect():
    return postgresql.dialect()


class TestModelDdlCompilesForPostgresql:
    def test_all_tables_compile(self):
        pg = _pg_dialect()
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=pg))
            assert ddl.strip().startswith("CREATE TABLE")

    def test_no_fts5_virtual_table_in_pg_ddl(self):
        pg = _pg_dialect()
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=pg))
            assert "VIRTUAL" not in ddl

    def test_all_indexes_compile(self):
        pg = _pg_dialect()
        for table in Base.metadata.sorted_tables:
            for index in table.indexes:
                str(CreateIndex(index).compile(dialect=pg))

    def test_datetime_columns_are_timestamptz(self):
        pg = _pg_dialect()
        for table in Base.metadata.sorted_tables:
            ddl = str(CreateTable(table).compile(dialect=pg))
            for column in table.columns:
                if isinstance(column.type, DateTime):
                    assert "WITH TIME ZONE" in ddl


class TestPartialUniqueIndex:
    def test_reminder_active_index_is_partial_on_both_dialects(self):
        from app.models.reminder import Reminder

        index = next(i for i in Reminder.__table__.indexes if i.name == "uq_reminders_active_task")
        pg_sql = str(CreateIndex(index).compile(dialect=_pg_dialect()))
        sqlite_sql = str(CreateIndex(index).compile(dialect=sqlite.dialect()))
        for sql in (pg_sql, sqlite_sql):
            assert "status IN ('PENDING', 'CLAIMED')" in sql, "必须是 partial unique index"


class TestMigrationOfflineCompile:
    """迁移链 PG 离线编译（--sql）：核心跨方言验证（对应 22 项之 5/6）。"""

    def _offline_upgrade_sql(self, url: str) -> str:
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            command.upgrade(cfg, "head", sql=True)
        return buf.getvalue()

    def test_postgres_offline_chain_complete_and_clean(self):
        sql_text = self._offline_upgrade_sql(_PG_TEST_URL)
        # 全链走到 head（5B：PG 分支执行 pgvector 迁移）
        assert "version_num='e6f5d4c3b2a1'" in sql_text
        # 5B：PG 分支创建 pg_chunk_vectors + HNSW cosine 索引（无 FTS5）
        assert "pg_chunk_vectors" in sql_text
        assert "CREATE INDEX IF NOT EXISTS ix_pg_chunk_vectors_embedding_hnsw" in sql_text
        assert "vector_cosine_ops" in sql_text
        assert "VECTOR(" in sql_text
        # PG 分支绝不创建 FTS5 虚拟表
        assert "VIRTUAL TABLE" not in sql_text
        # URL 密码不得泄漏进 SQL 输出
        assert _FAKE_PASSWORD not in sql_text
        # partial unique index 在 PG 输出中是 partial
        assert "status IN ('PENDING', 'CLAIMED')" in sql_text
        # 时间列是 TIMESTAMPTZ
        assert "WITH TIME ZONE" in sql_text
        # 4B 索引仍生成
        assert "ix_documents_index_status" in sql_text

    def test_postgres_offline_has_no_batch_recreate(self):
        sql_text = self._offline_upgrade_sql(_PG_TEST_URL)
        assert "ALTER TABLE task_proposals" in sql_text  # 原生 ALTER，而非 batch


class TestSqliteBranchKeepsFts5:
    def test_sqlite_upgrade_still_creates_fts5_table(self, tmp_path):
        """SQLite 分支继续 FTS5（对应 22 项之 7）：在线升级空库后虚拟表存在。"""
        db_file = tmp_path / "fts5_online.db"
        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
        command.upgrade(cfg, "head")

        engine = create_engine(f"sqlite:///{db_file}")
        with engine.connect() as conn:
            fts = conn.execute(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='document_chunks_fts'"
                )
            ).fetchone()
            assert fts is not None, "SQLite 分支必须继续创建 FTS5 虚拟表"


class TestHealthSplit:
    def test_health_is_liveness_probe_without_db(self, client):
        # 数据库层故障也不影响 /health（存活 ≠ 可用）
        def _boom():
            raise OperationalError(
                "SELECT 1", {}, Exception(f"refused {_PG_TEST_URL}")
            )

        app.dependency_overrides[get_db] = _boom
        try:
            r = client.get("/health")
        finally:
            app.dependency_overrides.clear()
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_health_db_reports_available(self, client):
        r = client.get("/health/db")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "database": "available"}

    def test_health_db_unavailable_sanitized_503(self, client):
        def _boom():
            raise OperationalError(
                "SELECT 1", {}, Exception(f"connection failed {_PG_TEST_URL}")
            )

        app.dependency_overrides[get_db] = _boom
        try:
            r = client.get("/health/db")
        finally:
            app.dependency_overrides.clear()
        assert r.status_code == 503
        assert r.json() == {
            "error": {"code": "SERVICE_UNAVAILABLE", "message": "Database is unavailable"}
        }
        assert _FAKE_PASSWORD not in r.text


class TestOperationalErrorHandler:
    def test_db_down_maps_to_stable_sanitized_503(self, client):
        """数据库不可用 → 所有走 DB 的路由返回稳定 503（22 项之 13/3）。"""

        def _boom():
            raise OperationalError(
                "SELECT 1", {}, Exception(f"psycopg error connecting {_PG_TEST_URL}")
            )

        app.dependency_overrides[get_db] = _boom
        try:
            r = client.get("/api/tasks")
        finally:
            app.dependency_overrides.clear()
        assert r.status_code == 503
        assert r.json() == {
            "error": {"code": "SERVICE_UNAVAILABLE", "message": "Database is unavailable"}
        }
        # 响应绝不包含连接串 / 密码 / SQL / 堆栈
        assert _FAKE_PASSWORD not in r.text
        assert "SELECT 1" not in r.text
        assert "Traceback" not in r.text
