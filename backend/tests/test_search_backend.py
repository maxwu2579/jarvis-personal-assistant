"""SearchBackend 单元测试（Phase 5A）：方言装配、ILIKE 回退纯函数、注入防护。

- build_keyword_sql 是纯函数：直接验证参数绑定、注入防护、候选上限、稳定排序；
- PostgreSQLSearchBackend.keyword_search 用桩 Session 验证「用户输入只进参数、
  返回结构稳定」；
- SQLiteSearchBackend（worker_env 上真实 FTS5）验证接线：建表/写入/查询/删除。
"""

from app.core.config import settings
from app.core.database import DIALECT
from app.core.dialect import DatabaseDialect
from app.search.base import SearchBackend
from app.search.factory import get_search_backend
from app.search.postgresql_backend import (
    MAX_QUERY_TOKENS,
    PostgreSQLSearchBackend,
    build_keyword_sql,
)


class _Row(dict):
    pass


class _Mappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _Mappings(self._rows)


class _StubDB:
    """记录 execute 的 (SQL, params)，按预设行返回；无需真实数据库。"""

    def __init__(self, rows):
        self._rows = rows
        self.calls: list[tuple[str, dict]] = []

    def execute(self, stmt, params):
        self.calls.append((str(stmt), dict(params)))
        return _Result(self._rows)


class TestFactoryDispatch:
    def test_backend_is_searchbackend_subclass(self):
        backend = get_search_backend()
        assert isinstance(backend, SearchBackend)

    def test_sqlite_dialect_uses_fts5_backend(self):
        # 测试进程的 DATABASE_URL 是 SQLite（conftest）→ 工厂必须分派 FTS5
        assert DIALECT is DatabaseDialect.SQLITE
        assert get_search_backend().backend_name == "fts5"

    def test_postgres_backend_identity(self):
        assert PostgreSQLSearchBackend().backend_name == "ilike-fallback"


class TestPostgresNoOps:
    """PG 回退无独立关键词索引：写路径为 no-op，且不抛错（可空 db）。"""

    def test_noop_methods_are_safe(self):
        backend = PostgreSQLSearchBackend()
        backend.ensure_keyword_index(None)
        backend.write_keyword_rows(None, 7, [(1, "content")])
        backend.delete_keyword_rows(None, 7)


class TestBuildKeywordSql:
    def test_empty_query_returns_none(self):
        sql, params = build_keyword_sql("", None, 10)
        assert sql is None and params == {}
        sql, params = build_keyword_sql("，。！", [1], 10)
        assert sql is None and params == {}

    def test_parameters_are_bound_not_interpolated(self):
        sql, params = build_keyword_sql("提醒", [1, 2], 10)
        assert "ILIKE :t0" in sql
        assert params["t0"] == "%提醒%"
        assert params["doc_0"] == 1 and params["doc_1"] == 2
        assert params["limit"] == 10
        assert "提醒" not in sql  # 用户文本绝不进入 SQL 文本

    def test_injection_query_is_neutralized(self):
        evil = "'; DROP TABLE documents; --"
        sql, params = build_keyword_sql(evil, None, 10)
        assert "';" not in sql
        assert "DROP TABLE" not in sql
        assert "; --" not in sql
        # 恶意文本只出现在绑定值里
        assert "%drop%" in params.values()

    def test_injection_document_ids_are_bound(self):
        evil_ids = ["1); DROP TABLE documents;--"]
        sql, params = build_keyword_sql("提醒", evil_ids, 10)
        assert "DROP" not in sql
        assert ");--" not in sql
        assert params["doc_0"] == "1); DROP TABLE documents;--"

    def test_candidate_limit_caps_sql_limit(self):
        sql, params = build_keyword_sql("提醒", None, 10**9)
        assert params["limit"] == settings.retrieval_candidate_limit

    def test_stable_ordering(self):
        sql, _ = build_keyword_sql("提醒", None, 10)
        assert (
            "ORDER BY score DESC, dc.document_id ASC, dc.chunk_index ASC" in sql
        )
        assert "LIMIT :limit" in sql

    def test_query_token_cap(self):
        many = " ".join(f"word{i}" for i in range(80))
        sql, params = build_keyword_sql(many, None, 10)
        # 每个 token 在 SQL 中出现两次（score CASE + OR 子句）
        assert sql.count("ILIKE") == MAX_QUERY_TOKENS * 2
        assert len([k for k in params if k.startswith("t")]) == MAX_QUERY_TOKENS
        assert "t50" not in params  # 超限 token 被截断

    def test_no_document_filter_when_ids_empty(self):
        sql, _ = build_keyword_sql("提醒", [], 10)
        assert "IN (" not in sql

    def test_indexed_filter_always_present(self):
        sql, _ = build_keyword_sql("提醒", None, 10)
        assert "d.index_status = 'INDEXED'" in sql


class TestPostgresKeywordSearch:
    def test_returns_stable_candidate_shape(self):
        backend = PostgreSQLSearchBackend()
        stub = _StubDB(
            [_Row(chunk_id=3, document_id=7, score=2), _Row(chunk_id=1, document_id=7, score=1)]
        )
        result = backend.keyword_search(
            stub, query="提醒 周报", document_ids=[7], limit=10
        )
        assert result == [
            {"chunk_id": 3, "document_id": 7, "score": 2.0},
            {"chunk_id": 1, "document_id": 7, "score": 1.0},
        ]
        assert all(isinstance(r["score"], float) for r in result)
        assert len(stub.calls) == 1
        _, params = stub.calls[0]
        assert params["t0"] == "%提醒%" and params["t1"] == "%周报%"
        assert params["limit"] == 10

    def test_no_tokens_returns_empty_without_sql(self):
        stub = _StubDB([])
        assert (
            PostgreSQLSearchBackend().keyword_search(
                stub, query="，。", document_ids=None, limit=10
            )
            == []
        )
        assert stub.calls == []


class TestSqliteBackendWiring:
    """在隔离 SQLite 库上验证 FTS5 后端接线（write→search→delete 全链路）。"""

    def test_fts5_write_search_delete_roundtrip(self, worker_env):
        from app.search.sqlite_backend import SQLiteSearchBackend

        _engine, SessionLocal = worker_env
        backend = SQLiteSearchBackend()
        db = SessionLocal()
        try:
            backend.ensure_keyword_index(db)
            backend.write_keyword_rows(
                db,
                document_id=7,
                rows=[(1, "提醒 周报 内容"), (2, "JARVIS 测试文档")],
            )
            hits = backend.keyword_search(
                db, query="提醒", document_ids=[7], limit=10
            )
            assert {h["chunk_id"] for h in hits} == {1}
            hits = backend.keyword_search(
                db, query="jarvis", document_ids=[7], limit=10
            )
            assert {h["chunk_id"] for h in hits} == {2}

            backend.delete_keyword_rows(db, 7)
            assert backend.keyword_search(db, query="提醒", document_ids=[7], limit=10) == []
        finally:
            db.close()

    def test_factory_backend_writes_via_fts5(self, worker_env):
        # 工厂返回的（SQLite）后端必须走 FTS5 写路径（替换 fts5.replace_document_rows）
        _engine, SessionLocal = worker_env
        backend = get_search_backend()
        assert backend.backend_name == "fts5"
        db = SessionLocal()
        try:
            backend.ensure_keyword_index(db)
            backend.write_keyword_rows(
                db, document_id=9, rows=[(5, "本地测试 汉语 文档")]
            )
            assert backend.keyword_search(db, query="汉语", document_ids=[9], limit=5)
        finally:
            db.close()
