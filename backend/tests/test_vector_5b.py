"""Phase 5B 离线/单元测试：pgvector SQL 纯函数、SQLite 向量后端、
readiness 检查、provider 契约、/health/db PG 分支（全程零网络）。

覆盖（对应 5B「离线/单元测试」）：
- build_vector_sql 纯函数：参数绑定、注入防护、稳定排序、候选上限、
  COUNT(*) OVER() total、INDEXED 过滤；
- PostgreSQLSearchBackend 向量写/查：桩 Session 验证「先 readiness 后执行」、
  upsert SQL、零向量拒绝、空行 no-op、候选结构稳定；
- SQLite 后端向量路径（隔离库）：写/查/删往返、幂等 upsert、聚合计数、
  精确 cosine Top-K、稳定 tie-break、document_ids 过滤、候选上限错误、
  非法 vector_json 跳过（不崩溃）；
- vector_readiness：parse_vector_dimension、五态检查顺序、
  raise 语义、head 与迁移文件一致；
- provider 契约：fake 确定性/维度/归一化/批次上限；openai 构造门控
  （未知模型/维度不合法/缺 Key）；错误码映射；mock 客户端批量顺序；
- _embed_chunk_batch 分批语义（embedding_batch_max）；
- /health/db 在 PG 分支的五种 readiness 响应（桩 DB，无真实 PG）。
"""

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.embeddings.base import (
    EmbeddingBatchTooLargeError,
    EmbeddingError,
    EmbeddingInternalError,
)
from app.embeddings.factory import create_embedding_provider
from app.embeddings.fake_provider import DeterministicFakeEmbeddingProvider
from app.models.document import (
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentIndexStatus,
    DocumentStatus,
)
from app.search.postgresql_backend import (
    PostgreSQLSearchBackend,
    build_vector_sql,
)
from app.search.sqlite_backend import SQLiteSearchBackend
from app.services.retrieval_errors import (
    RetrievalCandidateLimitExceededError,
    RetrievalError,
    VectorDimensionMismatchError,
)
from app.services.vector_readiness import (
    DB_UNAVAILABLE,
    EXPECTED_MIGRATION_HEAD,
    MIGRATION_NOT_CURRENT,
    VECTOR_DIMENSION_MISMATCH,
    VECTOR_EXTENSION_MISSING,
    VECTOR_SCHEMA_MISSING,
    check_pg_vector_readiness,
    parse_vector_dimension,
    raise_for_vector_readiness,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]
ALEMBIC_VERSIONS = BACKEND_DIR / "alembic" / "versions"


# ---------- 桩基础设施（与 test_search_backend.py 同风格） ----------


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

    def scalar(self):
        if not self._rows:
            return None
        row = self._rows[0]
        return row["value"] if isinstance(row, dict) and "value" in row else row


class _StubDB:
    """记录 execute 的 (SQL, params)，按预设行返回；无需真实数据库。"""

    def __init__(self, rows=None):
        self._rows = rows or []
        self.calls: list[tuple[str, dict]] = []

    def execute(self, stmt, params=None):
        # params 可能是 dict（单条）或 list[dict]（executemany），原样记录
        self.calls.append((str(stmt), params))
        return _Result(self._rows)


class _ReadinessStub:
    """vector_readiness 五态桩：按 SQL 内容返回预设值；fail=True 模拟连接失败。"""

    def __init__(
        self,
        *,
        version=EXPECTED_MIGRATION_HEAD,
        extension=True,
        table=True,
        type_name="vector(384)",
        fail=False,
    ):
        self._version = version
        self._extension = extension
        self._table = table
        self._type_name = type_name
        self._fail = fail
        self.calls: list[str] = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append(sql)
        if self._fail:
            raise SQLAlchemyError("simulated connection failure")
        if "alembic_version" in sql:
            return _Result([_Row(value=self._version)])
        if "pg_extension" in sql:
            return _Result([_Row(value=self._extension)])
        if "information_schema.tables" in sql:
            return _Result([_Row(value=self._table)])
        if "pg_attribute" in sql:
            return _Result([_Row(value=self._type_name)])
        return _Result([])


def _seed_doc(db, chunks_texts, *, index_status="INDEXED", title="vector-doc"):
    """直接插入 READY+INDEXED 文档与 chunks（不经过摄取服务）。"""
    doc = Document(
        original_filename=title,
        content_type="text/plain",
        size_bytes=sum(len(t) for t in chunks_texts),
        sha256="0" * 64,
        storage_key=uuid.uuid4().hex,
        status=DocumentStatus.READY.value,
        index_status=index_status,
    )
    db.add(doc)
    db.flush()
    chunks = []
    for i, content in enumerate(chunks_texts):
        chunk = DocumentChunk(
            document_id=doc.id,
            chunk_index=i,
            content=content,
            char_start=0,
            char_end=len(content),
            token_estimate=max(1, len(content) // 4),
        )
        db.add(chunk)
        db.flush()
        chunks.append(chunk)
    db.commit()
    return doc, chunks


def _vec(*values: float) -> list[float]:
    return list(values)


# ---------- A. build_vector_sql 纯函数 ----------


class TestBuildVectorSql:
    def test_score_and_total_expressions_present(self):
        sql, _ = build_vector_sql(None, 10)
        assert "(1 - (pv.embedding <=> CAST(:q AS vector))) AS score" in sql
        assert "COUNT(*) OVER() AS total" in sql
        assert "pg_chunk_vectors pv" in sql
        assert "d.index_status = 'INDEXED'" in sql

    def test_stable_ordering_clauses(self):
        sql, _ = build_vector_sql(None, 10)
        assert (
            "ORDER BY (pv.embedding <=> CAST(:q AS vector)) ASC, "
            "dc.document_id ASC, dc.chunk_index ASC, pv.chunk_id ASC "
            "LIMIT :limit" in sql
        )

    def test_document_ids_are_bound_not_interpolated(self):
        sql, params = build_vector_sql([1, 2], 10)
        assert "IN (:doc_0, :doc_1)" in sql
        assert params["doc_0"] == 1 and params["doc_1"] == 2
        assert params["limit"] == 10

    def test_injection_document_ids_are_bound(self):
        evil = ["1); DROP TABLE documents;--"]
        sql, params = build_vector_sql(evil, 10)
        assert "DROP" not in sql
        assert ");--" not in sql
        assert params["doc_0"] == "1); DROP TABLE documents;--"

    def test_no_in_clause_when_ids_empty(self):
        sql, _ = build_vector_sql([], 10)
        assert "IN (" not in sql

    def test_candidate_limit_caps_sql_limit(self):
        sql, params = build_vector_sql(None, 10**9)
        assert params["limit"] == settings.retrieval_candidate_limit

    def test_query_vector_bound_separately(self):
        # :q 由执行层单独绑定（列表 → SQL 内 CAST 为 vector），SQL 文本不含向量值
        sql, params = build_vector_sql(None, 5)
        assert ":q" in sql
        assert "CAST(:q AS vector)" in sql
        assert "q" not in params  # build 阶段不绑定查询向量


# ---------- B. PostgreSQLSearchBackend 向量 ops（桩 DB） ----------


class TestPostgresVectorStubs:
    def _readiness_spy(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "app.search.postgresql_backend.raise_for_vector_readiness",
            lambda db, dim: calls.append(dim),
        )
        return calls

    def test_vector_search_executes_readiness_then_query(self, monkeypatch):
        readiness = self._readiness_spy(monkeypatch)
        stub = _StubDB(
            [
                _Row(chunk_id=3, document_id=7, score=0.9, total=2),
                _Row(chunk_id=1, document_id=7, score=0.5, total=2),
            ]
        )
        candidates, total = PostgreSQLSearchBackend().vector_search(
            stub, query_vector=_vec(1.0, 0.0), document_ids=[7], limit=10
        )
        assert readiness == [2]  # readiness 收到的是 query 维度
        assert candidates == [
            {"chunk_id": 3, "document_id": 7, "score": 0.9},
            {"chunk_id": 1, "document_id": 7, "score": 0.5},
        ]
        assert total == 2
        assert len(stub.calls) == 1
        sql, params = stub.calls[0]
        assert "<=>" in sql and "LIMIT :limit" in sql
        assert params["q"] == [1.0, 0.0]

    def test_vector_search_empty_result_total_is_zero(self, monkeypatch):
        self._readiness_spy(monkeypatch)
        stub = _StubDB([])
        candidates, total = PostgreSQLSearchBackend().vector_search(
            stub, query_vector=_vec(1.0, 0.0), document_ids=None, limit=5
        )
        assert candidates == [] and total == 0

    def test_readiness_failure_propagates_before_execute(self, monkeypatch):
        def boom(db, dim):  # noqa: ARG001
            raise RetrievalError(
                VECTOR_EXTENSION_MISSING, "pgvector extension is not installed"
            )

        monkeypatch.setattr(
            "app.search.postgresql_backend.raise_for_vector_readiness", boom
        )
        stub = _StubDB()
        with pytest.raises(RetrievalError) as exc_info:
            PostgreSQLSearchBackend().vector_search(
                stub, query_vector=_vec(1.0), document_ids=None, limit=5
            )
        assert exc_info.value.code == VECTOR_EXTENSION_MISSING
        assert stub.calls == []  # 就绪失败：绝不执行查询（无静默回退）

    def test_write_zero_vector_rejected_before_sql(self, monkeypatch):
        self._readiness_spy(monkeypatch)
        stub = _StubDB()
        with pytest.raises(EmbeddingInternalError) as exc_info:
            PostgreSQLSearchBackend().write_vector_rows(
                stub,
                document_id=1,
                rows=[(1, [0.0, 0.0], "fake", "m", 2, "a" * 64)],
            )
        assert exc_info.value.code == "EMBEDDING_INTERNAL_ERROR"
        assert stub.calls == []  # 零向量：执行前拒绝，不留半行

    def test_write_upsert_sql_and_params(self, monkeypatch):
        readiness = self._readiness_spy(monkeypatch)
        stub = _StubDB()
        backend = PostgreSQLSearchBackend()
        backend.write_vector_rows(
            stub,
            document_id=5,
            rows=[
                (11, _vec(1.0, 0.5), "fake", "fake-deterministic-v1", 2, "ab" * 32),
                (12, _vec(0.0, 1.0), "fake", "fake-deterministic-v1", 2, "cd" * 32),
            ],
        )
        assert readiness == [2]
        assert len(stub.calls) == 1
        sql, params = stub.calls[0]
        assert "ON CONFLICT (chunk_id) DO UPDATE" in sql
        assert "VALUES (:chunk_id, :embedding" in sql
        # 批量参数按行展开（executemany 列表）
        assert params[0]["chunk_id"] == 11
        assert params[0]["embedding"] == [1.0, 0.5]
        assert params[0]["provider"] == "fake"
        assert params[1]["chunk_id"] == 12
        assert params[1]["dimension"] == 2
        assert params[0]["content_sha256"] == "ab" * 32
        assert params[0]["created_at"] is not None

    def test_write_empty_rows_is_noop(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "app.search.postgresql_backend.raise_for_vector_readiness",
            lambda db, dim: calls.append(dim),
        )
        stub = _StubDB()
        PostgreSQLSearchBackend().write_vector_rows(stub, document_id=5, rows=[])
        assert calls == [] and stub.calls == []

    def test_existing_vector_rows_shape(self):
        stub = _StubDB(
            [
                _Row(
                    chunk_id=3,
                    model="fake-deterministic-v1",
                    provider="fake",
                    dimension=384,
                    content_sha256="a" * 64,
                )
            ]
        )
        rows = PostgreSQLSearchBackend().existing_vector_rows(stub, 7)
        assert rows == {
            3: {
                "provider": "fake",
                "model": "fake-deterministic-v1",
                "dimension": 384,
                "content_sha256": "a" * 64,
            }
        }
        sql, params = stub.calls[0]
        assert "FROM pg_chunk_vectors" in sql
        assert params["doc_id"] == 7

    def test_count_vector_rows_aggregates(self):
        stub = _StubDB([_Row(document_id=7, n=3), _Row(document_id=9, n=1)])
        counts = PostgreSQLSearchBackend().count_vector_rows(stub, [7, 9])
        assert counts == {7: 3, 9: 1}
        sql, params = stub.calls[0]
        assert "FROM pg_chunk_vectors" in sql
        assert "GROUP BY" in sql
        assert params["doc_0"] == 7 and params["doc_1"] == 9

    def test_count_empty_ids_no_query(self):
        stub = _StubDB()
        assert PostgreSQLSearchBackend().count_vector_rows(stub, []) == {}
        assert stub.calls == []

    def test_delete_vector_rows_binds_document(self):
        stub = _StubDB()
        PostgreSQLSearchBackend().delete_vector_rows(stub, 7)
        sql, params = stub.calls[0]
        assert "DELETE FROM pg_chunk_vectors" in sql
        assert "SELECT id FROM document_chunks WHERE document_id = :doc_id" in sql
        assert params["doc_id"] == 7


# ---------- C. SQLite 向量后端（隔离库全链路） ----------


class TestSqliteVectorBackend:
    def _backend_and_db(self, worker_env):
        engine, SessionLocal = worker_env
        db = SessionLocal()
        return SQLiteSearchBackend(), db

    def test_write_read_delete_roundtrip(self, worker_env):
        backend, db = self._backend_and_db(worker_env)
        try:
            doc, chunks = _seed_doc(db, ["内容一", "内容二"])
            rows = [
                (chunks[0].id, _vec(1.0, 0.0), "fake", "fake-v1", 2, "a" * 64),
                (chunks[1].id, _vec(0.0, 1.0), "fake", "fake-v1", 2, "b" * 64),
            ]
            backend.write_vector_rows(db, doc.id, rows)
            existing = backend.existing_vector_rows(db, doc.id)
            assert set(existing) == {chunks[0].id, chunks[1].id}
            assert existing[chunks[0].id]["content_sha256"] == "a" * 64
            assert existing[chunks[1].id]["dimension"] == 2
            # vector_json 实际持久化（ORM 复核）
            row = (
                db.query(ChunkEmbedding)
                .filter(ChunkEmbedding.chunk_id == chunks[0].id)
                .one()
            )
            assert json.loads(row.vector_json) == [1.0, 0.0]
            assert row.model == "fake-v1"

            backend.delete_vector_rows(db, doc.id)
            assert backend.existing_vector_rows(db, doc.id) == {}
            backend.delete_vector_rows(db, doc.id)  # 幂等
        finally:
            db.close()

    def test_upsert_idempotent_no_duplicates(self, worker_env):
        backend, db = self._backend_and_db(worker_env)
        try:
            doc, chunks = _seed_doc(db, ["内容"])
            cid = chunks[0].id
            backend.write_vector_rows(
                db, doc.id, [(cid, _vec(1.0, 0.0), "fake", "v1", 2, "a" * 64)]
            )
            backend.write_vector_rows(
                db, doc.id, [(cid, _vec(0.0, 1.0), "fake", "v1", 2, "b" * 64)]
            )
            rows = db.query(ChunkEmbedding).filter(ChunkEmbedding.chunk_id == cid).all()
            assert len(rows) == 1  # 一对一约束：覆盖而非新增
            assert json.loads(rows[0].vector_json) == [0.0, 1.0]
        finally:
            db.close()

    def test_count_vector_rows_aggregates_per_document(self, worker_env):
        backend, db = self._backend_and_db(worker_env)
        try:
            doc_a, chunks_a = _seed_doc(db, ["a1", "a2"])
            doc_b, chunks_b = _seed_doc(db, ["b1"])
            backend.write_vector_rows(
                db,
                doc_a.id,
                [(c.id, _vec(1.0), "fake", "v1", 1, "a" * 64) for c in chunks_a],
            )
            backend.write_vector_rows(
                db,
                doc_b.id,
                [(c.id, _vec(1.0), "fake", "v1", 1, "b" * 64) for c in chunks_b],
            )
            assert backend.count_vector_rows(db, [doc_a.id, doc_b.id]) == {
                doc_a.id: 2,
                doc_b.id: 1,
            }
            assert backend.count_vector_rows(db, []) == {}
        finally:
            db.close()

    def test_vector_search_exact_cosine_topk(self, worker_env):
        backend, db = self._backend_and_db(worker_env)
        try:
            doc, chunks = _seed_doc(db, ["x", "y", "z"])
            backend.write_vector_rows(
                db,
                doc.id,
                [
                    (chunks[0].id, _vec(1.0, 0.0, 0.0), "fake", "v1", 3, "a" * 64),
                    (chunks[1].id, _vec(0.0, 1.0, 0.0), "fake", "v1", 3, "b" * 64),
                    (chunks[2].id, _vec(0.0, 0.0, 1.0), "fake", "v1", 3, "c" * 64),
                ],
            )
            candidates, total = backend.vector_search(
                db, query_vector=_vec(1.0, 0.0, 0.0), document_ids=None, limit=2
            )
            assert total == 3
            assert [c["chunk_id"] for c in candidates] == [chunks[0].id, chunks[1].id]
            assert candidates[0]["score"] == pytest.approx(1.0)
            assert candidates[1]["score"] == pytest.approx(0.0)
            # limit=1 截断
            candidates, _ = backend.vector_search(
                db, query_vector=_vec(1.0, 0.0, 0.0), document_ids=None, limit=1
            )
            assert len(candidates) == 1
        finally:
            db.close()

    def test_vector_search_stable_tie_break(self, worker_env):
        backend, db = self._backend_and_db(worker_env)
        try:
            doc_a, chunks_a = _seed_doc(db, ["a"], title="doc-a")
            doc_b, chunks_b = _seed_doc(db, ["b"], title="doc-b")
            # 两 chunk 向量完全相同 → 按 document_id 稳定排序
            backend.write_vector_rows(
                db, doc_a.id, [(chunks_a[0].id, _vec(1.0), "fake", "v1", 1, "a" * 64)]
            )
            backend.write_vector_rows(
                db, doc_b.id, [(chunks_b[0].id, _vec(1.0), "fake", "v1", 1, "b" * 64)]
            )
            first, total = backend.vector_search(
                db, query_vector=_vec(1.0), document_ids=None, limit=2
            )
            second, _ = backend.vector_search(
                db, query_vector=_vec(1.0), document_ids=None, limit=2
            )
            assert total == 2
            assert [c["chunk_id"] for c in first] == [c["chunk_id"] for c in second]
            assert [c["document_id"] for c in first] == sorted(
                c["document_id"] for c in first
            )  # 同分 → document_id 升序
        finally:
            db.close()

    def test_vector_search_document_filter(self, worker_env):
        backend, db = self._backend_and_db(worker_env)
        try:
            doc_a, chunks_a = _seed_doc(db, ["a"], title="doc-a")
            doc_b, chunks_b = _seed_doc(db, ["b"], title="doc-b")
            backend.write_vector_rows(
                db, doc_a.id, [(chunks_a[0].id, _vec(1.0), "fake", "v1", 1, "a" * 64)]
            )
            backend.write_vector_rows(
                db, doc_b.id, [(chunks_b[0].id, _vec(1.0), "fake", "v1", 1, "b" * 64)]
            )
            candidates, total = backend.vector_search(
                db, query_vector=_vec(1.0), document_ids=[doc_a.id], limit=10
            )
            assert total == 1
            assert candidates[0]["chunk_id"] == chunks_a[0].id
        finally:
            db.close()

    def test_vector_search_candidate_limit_error(self, worker_env, monkeypatch):
        backend, db = self._backend_and_db(worker_env)
        try:
            doc, chunks = _seed_doc(db, ["a", "b", "c"])
            backend.write_vector_rows(
                db,
                doc.id,
                [(c.id, _vec(1.0), "fake", "v1", 1, "a" * 64) for c in chunks],
            )
            monkeypatch.setattr(settings, "retrieval_candidate_limit", 2)
            with pytest.raises(RetrievalCandidateLimitExceededError):
                backend.vector_search(
                    db, query_vector=_vec(1.0), document_ids=None, limit=5
                )
        finally:
            db.close()

    def test_invalid_vector_json_skipped_not_crash(self, worker_env):
        backend, db = self._backend_and_db(worker_env)
        try:
            doc, chunks = _seed_doc(db, ["a", "b"])
            # 手工插入三类非法向量（绕过 write 校验，模拟历史/损坏数据）
            for chunk, raw in [
                (chunks[0].id, "not-json"),
                (chunks[1].id, "[1.0, 999.0, 0.5]"),  # 长度 3 ≠ 期望 2 → 跳过
            ]:
                db.add(
                    ChunkEmbedding(
                        chunk_id=chunk,
                        provider="fake",
                        model="v1",
                        dimension=2,
                        vector_json=raw,
                        content_sha256="a" * 64,
                    )
                )
            db.commit()
            candidates, total = backend.vector_search(
                db, query_vector=_vec(1.0, 0.0), document_ids=None, limit=10
            )
            assert total == 2  # 候选总数含非法行（扫描数语义）
            assert candidates == []  # 非法向量全部跳过，不崩溃
            # 非有限数值同样跳过
            db.add(
                ChunkEmbedding(
                    chunk_id=_seed_doc(db, ["c"])[1][0].id,
                    provider="fake",
                    model="v1",
                    dimension=2,
                    vector_json="[1.0, NaN]",
                    content_sha256="a" * 64,
                )
            )
            db.commit()
            candidates, total = backend.vector_search(
                db, query_vector=_vec(1.0, 0.0), document_ids=None, limit=10
            )
            assert candidates == []
        finally:
            db.close()

    def test_skips_non_indexed_documents(self, worker_env):
        backend, db = self._backend_and_db(worker_env)
        try:
            doc, chunks = _seed_doc(db, ["a"], index_status="INDEX_FAILED")
            backend.write_vector_rows(
                db, doc.id, [(chunks[0].id, _vec(1.0), "fake", "v1", 1, "a" * 64)]
            )
            candidates, total = backend.vector_search(
                db, query_vector=_vec(1.0), document_ids=None, limit=5
            )
            assert candidates == [] and total == 0
        finally:
            db.close()


# ---------- D. vector_readiness ----------


class TestVectorReadiness:
    def test_parse_vector_dimension(self):
        assert parse_vector_dimension("vector(384)") == 384
        assert parse_vector_dimension("vector(1536)") == 1536
        assert parse_vector_dimension("vector(384)") is not None
        assert parse_vector_dimension("VECTOR(384)") == 384
        assert parse_vector_dimension("vector") is None
        assert parse_vector_dimension("text") is None
        assert parse_vector_dimension(None) is None
        assert parse_vector_dimension("") is None

    def test_ready_returns_none(self):
        stub = _ReadinessStub()
        assert check_pg_vector_readiness(stub, 384) is None

    def test_db_unavailable_on_connection_failure(self):
        stub = _ReadinessStub(fail=True)
        assert check_pg_vector_readiness(stub, 384) == DB_UNAVAILABLE

    def test_migration_not_current(self):
        stub = _ReadinessStub(version="7c9d4e2f5a1b")
        assert check_pg_vector_readiness(stub, 384) == MIGRATION_NOT_CURRENT

    def test_migration_missing_table(self):
        stub = _ReadinessStub(version=None)
        assert check_pg_vector_readiness(stub, 384) == MIGRATION_NOT_CURRENT

    def test_extension_missing(self):
        stub = _ReadinessStub(extension=False)
        assert check_pg_vector_readiness(stub, 384) == VECTOR_EXTENSION_MISSING

    def test_schema_missing(self):
        stub = _ReadinessStub(table=False)
        assert check_pg_vector_readiness(stub, 384) == VECTOR_SCHEMA_MISSING

    def test_dimension_mismatch(self):
        stub = _ReadinessStub(type_name="vector(8)")
        assert check_pg_vector_readiness(stub, 384) == VECTOR_DIMENSION_MISMATCH
        stub = _ReadinessStub(type_name="vector")
        assert check_pg_vector_readiness(stub, 384) == VECTOR_DIMENSION_MISMATCH

    def test_raise_ready_no_op(self):
        raise_for_vector_readiness(_ReadinessStub(), 384)  # 不抛即通过

    def test_raise_failure_codes(self):
        with pytest.raises(RetrievalError) as exc_info:
            raise_for_vector_readiness(_ReadinessStub(version="old"), 384)
        assert exc_info.value.code == MIGRATION_NOT_CURRENT
        with pytest.raises(RetrievalError) as exc_info:
            raise_for_vector_readiness(_ReadinessStub(extension=False), 384)
        assert exc_info.value.code == VECTOR_EXTENSION_MISSING
        with pytest.raises(RetrievalError) as exc_info:
            raise_for_vector_readiness(_ReadinessStub(table=False), 384)
        assert exc_info.value.code == VECTOR_SCHEMA_MISSING

    def test_raise_dimension_mismatch_uses_dedicated_error(self):
        with pytest.raises(VectorDimensionMismatchError) as exc_info:
            raise_for_vector_readiness(_ReadinessStub(type_name="vector(8)"), 384)
        assert exc_info.value.code == VECTOR_DIMENSION_MISMATCH
        # 响应文案脱敏：不含 SQL/堆栈
        assert "atttypmod" not in exc_info.value.message

    def test_head_matches_latest_migration(self):
        # 用 alembic 自身的 ScriptDirectory 计算 head（权威，与迁移加载机制一致）
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        cfg = Config(str(BACKEND_DIR / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        heads = set(ScriptDirectory.from_config(cfg).get_heads())
        assert heads == {EXPECTED_MIGRATION_HEAD}
        # 每个迁移文件都声明自己的 revision 且在链上（无悬空文件）
        revisions = set()
        for path in ALEMBIC_VERSIONS.glob("*.py"):
            if path.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(path.stem, path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            revisions.add(mod.revision)
        assert EXPECTED_MIGRATION_HEAD in revisions
        assert len(revisions) == len(list(ALEMBIC_VERSIONS.glob("*.py")))


# ---------- E. fake provider 契约 ----------


class TestFakeProviderContract:
    def test_dimension_and_determinism(self):
        provider = DeterministicFakeEmbeddingProvider(dimension=8)
        assert provider.dimension == 8
        assert provider.model_name == "fake-deterministic-v1"
        v1 = provider.embed_query("hello world")
        v2 = provider.embed_query("hello world")
        assert v1 == v2  # 相同输入恒同输出（零随机）

    def test_vectors_are_unit_norm(self):
        provider = DeterministicFakeEmbeddingProvider(dimension=384)
        vector = provider.embed_query("norm check")
        norm = sum(x * x for x in vector) ** 0.5
        assert norm == pytest.approx(1.0)

    def test_distinct_inputs_give_distinct_vectors(self):
        provider = DeterministicFakeEmbeddingProvider(dimension=384)
        vectors = provider.embed_documents(["alpha", "beta", "gamma"])
        assert len(vectors) == 3
        assert len({tuple(v) for v in vectors}) == 3

    def test_batch_matches_query(self):
        provider = DeterministicFakeEmbeddingProvider(dimension=384)
        batch = provider.embed_documents(["same"])
        assert batch == [provider.embed_query("same")]

    def test_empty_batch(self):
        assert DeterministicFakeEmbeddingProvider(8).embed_documents([]) == []

    def test_batch_too_large_rejected(self, monkeypatch):
        monkeypatch.setattr(settings, "embedding_batch_max", 2)
        provider = DeterministicFakeEmbeddingProvider(dimension=8)
        with pytest.raises(EmbeddingBatchTooLargeError):
            provider.embed_documents(["a", "b", "c"])

    def test_factory_creates_by_name(self):
        provider = create_embedding_provider("fake", dimension=16)
        assert isinstance(provider, DeterministicFakeEmbeddingProvider)
        assert provider.dimension == 16

    def test_unknown_provider_fails_with_known_list(self):
        from app.embeddings.base import EmbeddingError

        with pytest.raises(EmbeddingError) as exc_info:
            create_embedding_provider("not-a-provider")
        assert exc_info.value.code == "UNKNOWN_EMBEDDING_PROVIDER"
        assert "openai" in exc_info.value.message
        assert "fake" in exc_info.value.message


# ---------- F. openai provider 构造门控 + mock 客户端 ----------


class TestOpenAiProviderGate:
    def _make(self, **kwargs):
        from app.embeddings.openai_provider import OpenAIEmbeddingProvider

        base = {
            "dimension": 384,
            "model": "text-embedding-3-small",
            "api_key": "sk-test",
            "timeout_seconds": 5.0,
        }
        base.update(kwargs)
        return OpenAIEmbeddingProvider(**base)

    def test_unknown_model_rejected(self):
        with pytest.raises(EmbeddingError) as exc_info:
            self._make(model="text-embedding-unicorn")
        assert exc_info.value.code == "EMBEDDING_UNKNOWN_MODEL"

    def test_dimension_not_allowed_for_model(self):
        with pytest.raises(EmbeddingError) as exc_info:
            self._make(dimension=999)
        assert exc_info.value.code == "EMBEDDING_DIMENSION_MISMATCH"

    def test_missing_api_key_rejected(self):
        with pytest.raises(EmbeddingError) as exc_info:
            self._make(api_key="")
        assert exc_info.value.code == "EMBEDDING_NOT_CONFIGURED"

    def test_missing_model_rejected(self):
        with pytest.raises(EmbeddingError) as exc_info:
            self._make(model="")
        assert exc_info.value.code == "EMBEDDING_NOT_CONFIGURED"

    def test_error_code_mapping(self):
        """Phase 5C：_classify_error → (稳定码, 是否可重试)。"""
        from app.embeddings.openai_provider import _classify_error

        class APITimeoutError(Exception):
            pass

        class RateLimitError(Exception):
            pass

        class AuthenticationError(Exception):
            pass

        class PermissionDeniedError(Exception):
            pass

        class NotFoundError(Exception):
            pass

        class APIStatusError(Exception):
            def __init__(self, status_code):
                self.status_code = status_code

        assert _classify_error(APITimeoutError()) == (
            "EMBEDDING_TIMEOUT",
            True,
        )
        assert _classify_error(RateLimitError()) == (
            "EMBEDDING_RATE_LIMITED",
            True,
        )
        assert _classify_error(AuthenticationError()) == (
            "EMBEDDING_INVALID_KEY",
            False,
        )
        assert _classify_error(PermissionDeniedError()) == (
            "EMBEDDING_INVALID_KEY",
            False,
        )
        assert _classify_error(NotFoundError()) == (
            "EMBEDDING_UPSTREAM_ERROR",
            False,  # 无 status_code 的 4xx：不重试
        )
        assert _classify_error(APIStatusError(500)) == (
            "EMBEDDING_UPSTREAM_ERROR",
            True,  # 5xx：可重试
        )
        assert _classify_error(APIStatusError(429)) == (
            "EMBEDDING_RATE_LIMITED",
            True,  # 429：可重试
        )
        assert _classify_error(APIStatusError(404)) == (
            "EMBEDDING_UPSTREAM_ERROR",
            False,  # 其他 4xx：不重试
        )

    def test_mocked_client_batch_order_and_dimensions_kwarg(self):
        from types import SimpleNamespace

        from app.embeddings.openai_provider import OpenAIEmbeddingProvider

        calls = {}

        class FakeResponse:
            def __init__(self):
                self.data = [
                    SimpleNamespace(index=1, embedding=[0.0] * 512),
                    SimpleNamespace(index=0, embedding=[1.0] * 512),
                ]

        class FakeClient:
            def __init__(self):
                self.embeddings = SimpleNamespace(create=lambda **kw: self._create(kw))

            def _create(self, kwargs):
                calls.update(kwargs)
                assert kwargs["input"] == ["second", "first"]
                return FakeResponse()

        provider = OpenAIEmbeddingProvider(
            dimension=512,
            model="text-embedding-3-small",
            api_key="sk-test",
            timeout_seconds=5.0,
            client_factory=lambda: FakeClient(),
        )
        vectors = provider._embed_batch(["second", "first"])
        assert calls["dimensions"] == 512  # 3-small 必须传 dimensions
        assert vectors[0] == [1.0] * 512  # 按输入顺序排序（index 0 是 second）
        assert vectors[1] == [0.0] * 512

    def test_mocked_client_response_count_mismatch(self):
        from types import SimpleNamespace

        from app.embeddings.openai_provider import OpenAIEmbeddingProvider

        class FakeClient:
            def __init__(self):
                self.embeddings = SimpleNamespace(
                    create=lambda **kw: SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[1.0] * 512)])
                )

        provider = OpenAIEmbeddingProvider(
            dimension=512,
            model="text-embedding-3-small",
            api_key="sk-test",
            timeout_seconds=5.0,
            client_factory=lambda: FakeClient(),
        )
        with pytest.raises(Exception) as exc_info:
            provider._embed_batch(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_RESPONSE_INVALID"

    def test_mocked_client_exception_maps_to_stable_error(self):
        from types import SimpleNamespace

        from app.embeddings.base import EmbeddingError
        from app.embeddings.openai_provider import OpenAIEmbeddingProvider

        class APITimeoutError(Exception):
            pass

        class FakeClient:
            def __init__(self):
                self.embeddings = SimpleNamespace(
                    create=lambda **kw: (_ for _ in ()).throw(APITimeoutError())
                )

        provider = OpenAIEmbeddingProvider(
            dimension=512,
            model="text-embedding-3-small",
            api_key="sk-test",
            timeout_seconds=5.0,
            client_factory=lambda: FakeClient(),
        )
        with pytest.raises(EmbeddingError) as exc_info:
            provider._embed_batch(["a"])
        assert exc_info.value.code == "EMBEDDING_TIMEOUT"
        assert "sk-test" not in exc_info.value.message  # Key 绝不进响应文案


# ---------- G. _embed_chunk_batch 分批语义 ----------


class TestEmbedChunkBatch:
    def test_splits_by_batch_max_preserving_order(self, monkeypatch):
        from app.services.retrieval_index_service import _embed_chunk_batch

        monkeypatch.setattr(settings, "embedding_batch_max", 2)
        sizes: list[int] = []
        expected = []

        class RecordingProvider(DeterministicFakeEmbeddingProvider):
            def embed_documents(self, texts):
                sizes.append(len(texts))
                result = super().embed_documents(texts)
                expected.extend(result)
                return result

        provider = RecordingProvider(dimension=8)
        contents = [f"text-{i}" for i in range(5)]
        vectors = _embed_chunk_batch(provider, contents)
        assert sizes == [2, 2, 1]  # 按 embedding_batch_max 分批
        assert vectors == expected  # 顺序与输入严格一致

    def test_empty_contents(self, monkeypatch):
        from app.services.retrieval_index_service import _embed_chunk_batch

        monkeypatch.setattr(settings, "embedding_batch_max", 2)
        assert _embed_chunk_batch(DeterministicFakeEmbeddingProvider(8), []) == []


# ---------- H. /health/db PG 分支（桩 DB） ----------


class TestHealthDbPgBranch:
    def _call_health_db(self, client, monkeypatch, stub):
        import app.main as main_module
        from app.core.database import get_db

        monkeypatch.setattr(main_module, "IS_SQLITE", False)
        monkeypatch.setitem(
            main_module.app.dependency_overrides, get_db, lambda: stub
        )
        return client.get("/health/db")

    def test_ready_returns_200(self, client, monkeypatch):
        stub = _ReadinessStub()
        resp = self._call_health_db(client, monkeypatch, stub)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["database"] == "available"
        assert body["pgvector"] == "ready"

    def test_db_unavailable_returns_503_stable_code(self, client, monkeypatch):
        stub = _ReadinessStub(fail=True)
        resp = self._call_health_db(client, monkeypatch, stub)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == DB_UNAVAILABLE

    def test_migration_stale_returns_503(self, client, monkeypatch):
        stub = _ReadinessStub(version="7c9d4e2f5a1b")
        resp = self._call_health_db(client, monkeypatch, stub)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == MIGRATION_NOT_CURRENT

    def test_extension_missing_returns_503(self, client, monkeypatch):
        stub = _ReadinessStub(extension=False)
        resp = self._call_health_db(client, monkeypatch, stub)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == VECTOR_EXTENSION_MISSING

    def test_dimension_mismatch_returns_503(self, client, monkeypatch):
        stub = _ReadinessStub(type_name="vector(8)")
        resp = self._call_health_db(client, monkeypatch, stub)
        assert resp.status_code == 503
        assert resp.json()["error"]["code"] == VECTOR_DIMENSION_MISMATCH

    def test_response_never_leaks_connection_details(self, client, monkeypatch):
        stub = _ReadinessStub(fail=True)
        resp = self._call_health_db(client, monkeypatch, stub)
        raw = resp.text
        assert "sqlite" not in raw
        assert "DATABASE_URL" not in raw
        assert "password" not in raw.lower()
