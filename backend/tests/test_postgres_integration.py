"""PostgreSQL 真实集成冒烟（Phase 5A 第八节：显式门控，默认 SKIPPED）。

环境门控：
- 必须同时设置 RUN_POSTGRES_INTEGRATION=1 与 TEST_POSTGRES_URL
  （指向专用测试库，例如 postgresql+psycopg://user:pw@127.0.0.1:5432/jarvis_5a_test）；
- 未设置 RUN_POSTGRES_INTEGRATION=1 → 整模块 SKIPPED（绝不连接、绝不伪造 PASSED）；
- 已启用但 URL 缺失 → SKIP 并给出明确指引；
- 测试库名命中生产库名单（postgres / template0 / template1 / jarvis）→ 直接 FAIL；
- 连接前打印脱敏 host/port/database（绝不含密码）。

验证内容：迁移链在真实 PG 上可执行、PG 分支不建 FTS5、partial unique index
真实生效、TIMESTAMPTZ 往返 aware UTC、ILIKE 回退端到端检索、FK CASCADE。
"""

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from alembic import command
from alembic.config import Config

BACKEND_DIR = Path(__file__).resolve().parents[1]

RUN = os.environ.get("RUN_POSTGRES_INTEGRATION") == "1"
TEST_URL = os.environ.get("TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not RUN,
    reason="RUN_POSTGRES_INTEGRATION=1 未设置：真实 PG 冒烟默认跳过",
)

# 生产库名黑名单：在这些库上运行集成测试是危险的，直接失败而不是跳过
_FORBIDDEN_DB_NAMES = {"postgres", "template0", "template1", "jarvis"}

_EXPECTED_TABLES = {
    "tasks",
    "conversations",
    "messages",
    "task_proposals",
    "reminders",
    "notifications",
    "documents",
    "document_chunks",
    "chunk_embeddings",
    "rag_answers",
    "pg_chunk_vectors",
}


def _make_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture(scope="module")
def pg_env():
    """验证门控 → 连接测试库 → 跑迁移链 → 提供 (engine, SessionLocal) + 清理。"""
    if not TEST_URL:
        pytest.skip(
            "RUN_POSTGRES_INTEGRATION=1 但 TEST_POSTGRES_URL 未设置 —— "
            "请提供专用测试库 URL（拒绝生产库名）"
        )
    from sqlalchemy.engine import make_url

    url = make_url(TEST_URL)
    db_name = url.database or ""
    if db_name in _FORBIDDEN_DB_NAMES:
        pytest.fail(f"拒绝在疑似生产库上运行集成测试: {db_name!r} —— 请提供专用测试库")
    # 脱敏连接信息（绝不含密码）
    print(
        f"[pg-integration] host={url.host} port={url.port} "
        f"database={db_name} driver={url.drivername}"
    )

    engine = create_engine(TEST_URL, pool_pre_ping=True)
    # 与生产 app/core/database.py 一致：为每个连接注册 pgvector 适配器
    # （vector 类型加载/编码，并让 INSERT 的 list 参数走赋值 cast）。
    # 测试 engine 直接 create_engine 而来，不带生产 engine 的 connect
    # listener，必须在此补上。注意：表达式上下文（`<=>` 操作符）不会自动
    # 应用 array→vector 的 ASSIGNMENT cast，该问题由 build_vector_sql 的
    # CAST(:q AS vector) 在 SQL 层解决（见 app/search/postgresql_backend.py）。
    from pgvector.psycopg import register_vector

    @event.listens_for(engine, "connect")
    def _register_pg_vector_adapter(dbapi_connection, connection_record):
        register_vector(dbapi_connection)

    # 迁移链在真实 PG 上从空库执行到 head
    command.upgrade(_make_config(TEST_URL), "head")
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        yield engine, SessionLocal
    finally:
        from app.core.database import Base

        # pg_chunk_vectors 不在 Base.metadata（PG 专属表，Phase 5B），
        # 且其 FK 引用 document_chunks——必须先删除它，drop_all 才能
        # 正常移除其余表（否则 DependentObjectsStillExist）。
        # alembic_version 同理（Alembic 内部表，不删会误判已到 head）。
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS pg_chunk_vectors CASCADE"))
        Base.metadata.drop_all(bind=engine)
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        engine.dispose()


class TestMigrationChainOnPostgres:
    def test_upgrade_head_creates_full_schema_without_fts5(self, pg_env):
        engine, _ = pg_env
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert _EXPECTED_TABLES <= tables
        # PG 分支绝不创建 FTS5 虚拟表
        assert "document_chunks_fts" not in tables
        with engine.connect() as conn:
            version = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar()
            # 6F 修正：Phase 6A-6C 新增迁移后 head 前进到 8c9d0e1f2a3b4
            assert version == "8c9d0e1f2a3b4"
            # 5B：vector extension 已安装，HNSW cosine 索引存在
            extension = conn.execute(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")
            ).scalar()
            assert extension == 1
        indexes = inspector.get_indexes("pg_chunk_vectors")
        # inspector 对 HNSW 返回索引（access_method 为 hnsw）
        assert any(
            ix["column_names"] == ["embedding"] for ix in indexes
        )


class TestPostgresSemantics:
    def test_partial_unique_index_enforced(self, pg_env):
        """同一 Task 只能有一个活动提醒；已投递后可再创建（partial 语义真实生效）。"""
        from app.models.reminder import Reminder, ReminderStatus
        from app.models.task import Task, TaskStatus

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            task = Task(title="partial-index 测试", status=TaskStatus.CONFIRMED.value)
            db.add(task)
            db.commit()

            def _add_reminder(status: str) -> Reminder:
                reminder = Reminder(
                    task_id=task.id,
                    status=status,
                    remind_at=datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc),
                )
                db.add(reminder)
                db.commit()
                db.refresh(reminder)
                return reminder

            first = _add_reminder(ReminderStatus.PENDING.value)
            # 第二个活动提醒 → 违反 partial unique → 必须失败
            with pytest.raises(IntegrityError):
                db.add(
                    Reminder(
                        task_id=task.id,
                        status=ReminderStatus.PENDING.value,
                        remind_at=datetime(2026, 8, 12, 5, 0, tzinfo=timezone.utc),
                    )
                )
                db.commit()
            db.rollback()
            # 投递后释放槽位 → 可再次创建
            first.status = ReminderStatus.DELIVERED.value
            db.commit()
            _add_reminder(ReminderStatus.PENDING.value)
        finally:
            db.close()

    def test_datetime_roundtrip_stays_aware_utc(self, pg_env):
        """TIMESTAMPTZ 往返：写 aware UTC，读回仍是 aware UTC（与 SQLite naive 约定区分）。"""
        from app.models.reminder import Reminder, ReminderStatus
        from app.models.task import Task, TaskStatus

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            task = Task(title="tz 往返", status=TaskStatus.CONFIRMED.value)
            db.add(task)
            db.commit()
            original = datetime(2026, 8, 12, 4, 0, 30, 123456, tzinfo=timezone.utc)
            reminder = Reminder(
                task_id=task.id,
                status=ReminderStatus.PENDING.value,
                remind_at=original,
            )
            db.add(reminder)
            db.commit()
            read_back = (
                db.query(Reminder).filter(Reminder.id == reminder.id).one().remind_at
            )
            assert read_back.tzinfo is not None
            assert read_back.utcoffset().total_seconds() == 0
            assert read_back == original
        finally:
            db.close()

    def test_foreign_key_cascade_on_postgres(self, pg_env):
        """FK CASCADE 在 PG 上真实生效（删除任务级联删除提醒与通知）。"""
        from app.models.reminder import Notification, Reminder, ReminderStatus
        from app.models.task import Task, TaskStatus

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            task = Task(title="cascade 测试", status=TaskStatus.CONFIRMED.value)
            db.add(task)
            db.commit()
            reminder = Reminder(
                task_id=task.id,
                status=ReminderStatus.PENDING.value,
                remind_at=datetime(2026, 8, 12, 4, 0, tzinfo=timezone.utc),
            )
            db.add(reminder)
            db.commit()
            reminder_id = reminder.id
            db.add(
                Notification(
                    reminder_id=reminder_id,
                    task_id=task.id,
                    type="REMINDER_DUE",
                    title="t",
                    body="b",
                )
            )
            db.commit()
            db.delete(task)
            db.commit()
            # 级联删除后按已捕获的 id 查询（实例已删除，不可再访问属性）
            assert db.query(Reminder).filter(Reminder.id == reminder_id).count() == 0
            assert db.query(Notification).count() == 0
        finally:
            db.close()

    def test_ilike_fallback_search_end_to_end(self, pg_env):
        """ILIKE 兼容回退在真实 PG 上端到端工作（诚实 fallback，非 FTS）。"""
        from app.models.document import (
            Document,
            DocumentChunk,
            DocumentIndexStatus,
            DocumentStatus,
        )
        from app.search.postgresql_backend import PostgreSQLSearchBackend

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            doc = Document(
                original_filename="pg.txt",
                content_type="text/plain",
                size_bytes=10,
                sha256="c" * 64,
                storage_key="d" * 32,
                status=DocumentStatus.READY.value,
                index_status=DocumentIndexStatus.INDEXED.value,
            )
            db.add(doc)
            db.commit()
            db.add_all(
                [
                    DocumentChunk(
                        document_id=doc.id,
                        chunk_index=0,
                        content="JARVIS 提醒 周报 测试内容",
                        char_start=0,
                        char_end=12,
                        token_estimate=4,
                    ),
                    DocumentChunk(
                        document_id=doc.id,
                        chunk_index=1,
                        content="另一个完全无关的段落",
                        char_start=0,
                        char_end=10,
                        token_estimate=3,
                    ),
                ]
            )
            db.commit()

            backend = PostgreSQLSearchBackend()
            hits = backend.keyword_search(
                db, query="提醒", document_ids=[doc.id], limit=10
            )
            assert hits, "ILIKE 回退必须能命中"
            assert {h["chunk_id"] for h in hits} == {1}
            for h in hits:
                assert set(h) == {"chunk_id", "document_id", "score"}
                assert isinstance(h["score"], float)
            # 不相关查询 → 空结果（诚实返回，不硬凑）
            assert (
                backend.keyword_search(
                    db, query="不存在的词汇xyz", document_ids=[doc.id], limit=10
                )
                == []
            )
            # 仅索引文档可见：未 INDEXED 的文档不参与
            raw = db.query(DocumentChunk).filter(
                DocumentChunk.document_id == doc.id,
                DocumentChunk.chunk_index == 1,
            ).one()
            raw.content = "提醒 混入未索引文档"
            db.commit()
            doc.index_status = DocumentIndexStatus.NOT_INDEXED.value
            db.commit()
            assert (
                backend.keyword_search(
                    db, query="提醒", document_ids=[doc.id], limit=10
                )
                == []
            )
        finally:
            db.close()


# ---------- Phase 5B：pgvector 原生向量（门控集成） ----------

_PGV_TABLE = "pg_chunk_vectors"
_PGV_INDEX = "ix_pg_chunk_vectors_embedding_hnsw"


def _seed_ready_doc(db, texts, title="pg-vector-doc"):
    """直接插入 READY 文档与 chunks（不经过摄取服务；索引由服务层驱动）。"""
    import uuid

    from app.models.document import (
        Document,
        DocumentChunk,
        DocumentIndexStatus,
        DocumentStatus,
    )

    doc = Document(
        original_filename=title,
        content_type="text/plain",
        size_bytes=sum(len(t) for t in texts),
        sha256="0" * 64,
        storage_key=uuid.uuid4().hex,
        status=DocumentStatus.READY.value,
        index_status=DocumentIndexStatus.NOT_INDEXED.value,
    )
    db.add(doc)
    db.flush()
    chunks = []
    for i, content in enumerate(texts):
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


class TestPgVectorSemantics:
    """pgvector 真实集成：迁移产物、就绪检查、索引写入/检索往返、错误语义。"""

    @pytest.fixture(autouse=True)
    def _pg_backend(self, pg_env, monkeypatch):
        """把 SearchBackend 分发切到 PG：factory 用 `from ... import DIALECT`
        在导入时复制了值（模块全局副本），且 get_search_backend 有进程级
        缓存——必须重设 factory 模块自己的 DIALECT + 清缓存。
        否则测试会拿到 SQLite 后端对 PG session 执行（FTS5 SQL 报错）。"""
        from app.core.config import settings
        from app.core.dialect import DatabaseDialect
        from app.search import factory as factory_module

        monkeypatch.setattr(settings, "database_url", TEST_URL)
        monkeypatch.setattr(factory_module, "DIALECT", DatabaseDialect.POSTGRESQL)
        factory_module.get_search_backend.cache_clear()
        # module-scope pg_env 共享同一测试库：清空向量表，
        # 保证「表空/仅含本测试行」的断言不受前序测试累积行影响
        engine, _ = pg_env
        with engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {_PGV_TABLE}"))
        try:
            yield
        finally:
            factory_module.get_search_backend.cache_clear()

    def _dim(self):
        from app.core.config import settings

        return settings.embedding_dimension

    def _fake_provider(self):
        from app.embeddings.fake_provider import DeterministicFakeEmbeddingProvider

        return DeterministicFakeEmbeddingProvider(dimension=self._dim())

    def test_readiness_ready_after_migration(self, pg_env):
        from app.services.vector_readiness import check_pg_vector_readiness

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            assert check_pg_vector_readiness(db, self._dim()) is None
            # 列类型确为 VECTOR(配置维度)
            type_name = db.execute(
                text(
                    "SELECT format_type(a.atttypid, a.atttypmod) "
                    "FROM pg_catalog.pg_attribute a "
                    "WHERE a.attrelid = 'pg_chunk_vectors'::regclass "
                    "AND a.attname = 'embedding'"
                )
            ).scalar()
            assert type_name == f"vector({self._dim()})"
        finally:
            db.close()

    def test_index_and_vector_search_roundtrip(self, pg_env):
        """索引服务写入 → pg_chunk_vectors 行与 chunks 一一对应 →
        向量检索精确命中（fake provider 使最近邻可精确构造）。"""
        from app.search.factory import get_search_backend
        from app.services.retrieval_index_service import RetrievalIndexService

        engine, SessionLocal = pg_env
        db = SessionLocal()
        backend = get_search_backend()
        assert backend.vector_backend_name == "pgvector-cosine"
        try:
            texts = ["查询向量目标内容", "完全不相关的内容填充", "第三个普通片段"]
            doc, chunks = _seed_ready_doc(db, texts)
            stats = RetrievalIndexService(db, self._fake_provider()).index_document(
                doc.id
            )
            assert stats.embeddings_created == len(chunks)
            assert stats.embeddings_skipped == 0

            rows = db.execute(
                text(
                    "SELECT chunk_id, provider, model_name, dimension "
                    f"FROM {_PGV_TABLE} WHERE chunk_id IN ("
                    "SELECT id FROM document_chunks WHERE document_id = :d)"
                ),
                {"d": doc.id},
            ).mappings().all()
            assert len(rows) == len(chunks)
            assert {r["provider"] for r in rows} == {
                self._fake_provider().provider_name  # "fake"（Phase 5C 稳定标识）
            }
            assert {r["dimension"] for r in rows} == {self._dim()}

            # 向量检索：query == chunk 文本 → 精确 top-1
            from app.services.retrieval_service import RetrievalService

            result = RetrievalService(db, self._fake_provider()).search(
                query=texts[0], mode="vector", top_k=1
            )
            assert result.items[0].chunk_id == chunks[0].id
            assert result.items[0].vector_score == pytest.approx(1.0)
            assert result.total_candidates == len(chunks)

            # document_ids 过滤
            result = RetrievalService(db, self._fake_provider()).search(
                query=texts[0], mode="vector", top_k=5, document_ids=[doc.id]
            )
            assert result.total_candidates == len(chunks)
        finally:
            db.close()

    def test_reindex_skip_and_update_semantics(self, pg_env):
        """内容未变 → skipped；内容变化 → updated（行数恒定，无重复）。"""
        from app.services.retrieval_index_service import RetrievalIndexService

        engine, SessionLocal = pg_env
        db = SessionLocal()
        provider = self._fake_provider()
        try:
            texts = ["第一段稳定内容", "第二段稳定内容"]
            doc, chunks = _seed_ready_doc(db, texts)
            service = RetrievalIndexService(db, provider)
            service.index_document(doc.id)

            again = service.index_document(doc.id)
            assert again.embeddings_skipped == len(chunks)
            assert again.embeddings_created == 0
            assert again.embeddings_updated == 0
            count = db.execute(
                text(f"SELECT COUNT(*) FROM {_PGV_TABLE}")
            ).scalar()
            assert count == len(chunks)

            # 修改 chunk 内容 → updated（upsert 覆盖，行数不变）
            from app.models.document import DocumentChunk

            chunk = db.query(DocumentChunk).filter(
                DocumentChunk.id == chunks[0].id
            ).one()
            chunk.content = "第一段内容已更新"
            db.commit()
            updated = service.index_document(doc.id)
            assert updated.embeddings_updated == 1
            assert updated.embeddings_skipped == len(chunks) - 1
            count = db.execute(
                text(f"SELECT COUNT(*) FROM {_PGV_TABLE}")
            ).scalar()
            assert count == len(chunks)  # upsert：无重复行
        finally:
            db.close()

    def test_force_reindex_rewrites_without_duplicates(self, pg_env):
        from app.services.retrieval_index_service import RetrievalIndexService

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            texts = ["强制重建内容"]
            doc, chunks = _seed_ready_doc(db, texts)
            service = RetrievalIndexService(db, self._fake_provider())
            service.index_document(doc.id)
            stats = service.reindex_document(doc.id)
            assert stats.embeddings_created == len(chunks)
            count = db.execute(
                text(f"SELECT COUNT(*) FROM {_PGV_TABLE}")
            ).scalar()
            assert count == len(chunks)
        finally:
            db.close()

    def test_dimension_mismatch_fails_index_atomically(self, pg_env):
        """provider 维度 ≠ 表列维度 → 稳定 503 语义 + 回滚（无半套数据）。"""
        from app.embeddings.fake_provider import DeterministicFakeEmbeddingProvider
        from app.services.retrieval_errors import IndexingFailedError
        from app.services.retrieval_index_service import RetrievalIndexService

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            doc, chunks = _seed_ready_doc(db, ["维度不匹配"])
            service = RetrievalIndexService(
                db, DeterministicFakeEmbeddingProvider(dimension=8)
            )
            with pytest.raises(IndexingFailedError) as exc_info:
                service.index_document(doc.id)
            assert exc_info.value.code == "INDEXING_FAILED"
            from app.models.document import DocumentIndexStatus

            db.refresh(doc)
            assert doc.index_status == DocumentIndexStatus.INDEX_FAILED.value
            # sanitized 文案：小写短语（不含代码串/维度以外的细节）
            assert "dimension mismatch" in doc.index_error_message
            assert db.execute(
                text(f"SELECT COUNT(*) FROM {_PGV_TABLE}")
            ).scalar() == 0  # 整体回滚
        finally:
            db.close()

    def test_zero_vector_rejected_atomically(self, pg_env, monkeypatch):
        """零向量 → HNSW 无法索引 → 显式拒绝 → 回滚 + INDEX_FAILED。"""
        from app.services.retrieval_errors import IndexingFailedError
        from app.services.retrieval_index_service import RetrievalIndexService

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            doc, chunks = _seed_ready_doc(db, ["零向量内容"])
            dim = self._dim()

            def zero_vectors(provider, contents):
                return [[0.0] * dim for _ in contents]

            monkeypatch.setattr(
                "app.services.retrieval_index_service._embed_chunk_batch",
                zero_vectors,
            )
            with pytest.raises(IndexingFailedError):
                RetrievalIndexService(db, self._fake_provider()).index_document(doc.id)
            from app.models.document import DocumentIndexStatus

            db.refresh(doc)
            assert doc.index_status == DocumentIndexStatus.INDEX_FAILED.value
            assert db.execute(
                text(f"SELECT COUNT(*) FROM {_PGV_TABLE}")
            ).scalar() == 0
        finally:
            db.close()

    def test_delete_document_index_removes_vector_rows(self, pg_env):
        from app.services.retrieval_index_service import RetrievalIndexService

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            doc, chunks = _seed_ready_doc(db, ["删除索引内容"])
            service = RetrievalIndexService(db, self._fake_provider())
            service.index_document(doc.id)
            assert db.execute(
                text(f"SELECT COUNT(*) FROM {_PGV_TABLE}")
            ).scalar() == len(chunks)
            service.delete_document_index(doc.id)
            assert db.execute(
                text(f"SELECT COUNT(*) FROM {_PGV_TABLE}")
            ).scalar() == 0
        finally:
            db.close()

    def test_keyword_vector_hybrid_modes_on_pg(self, pg_env):
        """PG 上三种检索模式端到端：ILIKE 回退 + pgvector + RRF 融合。"""
        from app.services.retrieval_index_service import RetrievalIndexService
        from app.services.retrieval_service import RetrievalService

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            texts = ["JARVIS 提醒系统 记录任务", "周末去公园散步 无关内容"]
            doc, chunks = _seed_ready_doc(db, texts)
            provider = self._fake_provider()
            RetrievalIndexService(db, provider).index_document(doc.id)
            service = RetrievalService(db, provider)

            kw = service.search(query="提醒", mode="keyword", top_k=5)
            assert kw.items and kw.items[0].chunk_id == chunks[0].id
            assert kw.items[0].keyword_score > 0

            vec = service.search(query=texts[1], mode="vector", top_k=5)
            assert vec.items and vec.items[0].chunk_id == chunks[1].id

            hybrid = service.search(query=texts[1], mode="hybrid", top_k=5)
            assert hybrid.items  # 单侧无结果时另一侧仍可返回
            assert hybrid.items[0].chunk_id == chunks[1].id
            assert hybrid.total_candidates >= 1
        finally:
            db.close()

    def test_provider_change_triggers_reembed(self, pg_env):
        """换 provider（model_name 变化）→ 身份过期 → stale 重嵌（Phase 5C）。"""
        from app.embeddings.fake_provider import DeterministicFakeEmbeddingProvider
        from app.services.retrieval_index_service import RetrievalIndexService

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            doc, chunks = _seed_ready_doc(db, ["换 provider 内容"])
            RetrievalIndexService(db, self._fake_provider()).index_document(doc.id)

            class OtherFake(DeterministicFakeEmbeddingProvider):
                model_name = "fake-deterministic-v2"

            stats = RetrievalIndexService(
                db, OtherFake(dimension=self._dim())
            ).index_document(doc.id)
            assert stats.embeddings_stale == len(chunks)
            model = db.execute(
                text(
                    f"SELECT model_name FROM {_PGV_TABLE} "
                    "WHERE chunk_id = :cid"
                ),
                {"cid": chunks[0].id},
            ).scalar()
            assert model == "fake-deterministic-v2"
        finally:
            db.close()

    def test_count_vector_rows_via_document_service(self, pg_env):
        from app.services.document_service import indexed_chunk_counts
        from app.services.retrieval_index_service import RetrievalIndexService

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            doc, chunks = _seed_ready_doc(db, ["计数一", "计数二", "计数三"])
            RetrievalIndexService(db, self._fake_provider()).index_document(doc.id)
            assert indexed_chunk_counts(db, [doc.id]) == {doc.id: 3}
            assert indexed_chunk_counts(db, []) == {}
        finally:
            db.close()

    def test_readiness_extension_missing(self, pg_env):
        """DROP EXTENSION（CASCADE 连带索引）→ VECTOR_EXTENSION_MISSING；
        finally 恢复 extension + HNSW 索引。"""
        from app.services.vector_readiness import (
            VECTOR_EXTENSION_MISSING,
            check_pg_vector_readiness,
        )

        engine, SessionLocal = pg_env
        db = SessionLocal()
        dim = self._dim()
        try:
            db.execute(text("DROP EXTENSION IF EXISTS vector CASCADE"))
            db.commit()
            assert (
                check_pg_vector_readiness(db, dim) == VECTOR_EXTENSION_MISSING
            )
        finally:
            db.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            # DROP EXTENSION CASCADE 会连带删除 embedding 列 → 先补回列再建索引
            db.execute(
                text(
                    f"ALTER TABLE {_PGV_TABLE} "
                    f"ADD COLUMN IF NOT EXISTS embedding VECTOR({dim})"
                )
            )
            db.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {_PGV_INDEX} "
                    f"ON {_PGV_TABLE} USING hnsw (embedding vector_cosine_ops)"
                )
            )
            db.commit()
            assert check_pg_vector_readiness(db, dim) is None  # 环境已恢复
            db.close()

    def test_readiness_schema_missing(self, pg_env):
        """DROP TABLE → VECTOR_SCHEMA_MISSING；finally 恢复表。"""
        from app.services.vector_readiness import (
            VECTOR_SCHEMA_MISSING,
            check_pg_vector_readiness,
        )

        engine, SessionLocal = pg_env
        db = SessionLocal()
        dim = self._dim()
        try:
            db.execute(text(f"DROP TABLE {_PGV_TABLE}"))
            db.commit()
            assert (
                check_pg_vector_readiness(db, dim) == VECTOR_SCHEMA_MISSING
            )
        finally:
            db.execute(
                text(
                    f"CREATE TABLE {_PGV_TABLE} ("
                    "chunk_id INTEGER PRIMARY KEY "
                    "REFERENCES document_chunks(id) ON DELETE CASCADE, "
                    f"embedding VECTOR({dim}) NOT NULL, "
                    "provider VARCHAR(50) NOT NULL, "
                    "model_name VARCHAR(100) NOT NULL, "
                    "dimension INTEGER NOT NULL, "
                    "content_sha256 VARCHAR(64) NOT NULL, "
                    "created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
                    "updated_at TIMESTAMP WITH TIME ZONE NOT NULL)"
                )
            )
            db.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS {_PGV_INDEX} "
                    f"ON {_PGV_TABLE} USING hnsw (embedding vector_cosine_ops)"
                )
            )
            db.commit()
            assert check_pg_vector_readiness(db, dim) is None
            db.close()

    def test_readiness_migration_stale(self, pg_env):
        """alembic_version 回退到旧版本 → MIGRATION_NOT_CURRENT；finally 恢复。"""
        from app.services.vector_readiness import (
            MIGRATION_NOT_CURRENT,
            check_pg_vector_readiness,
        )

        engine, SessionLocal = pg_env
        db = SessionLocal()
        dim = self._dim()
        try:
            db.execute(
                text("UPDATE alembic_version SET version_num = '7c9d4e2f5a1b'")
            )
            db.commit()
            assert (
                check_pg_vector_readiness(db, dim) == MIGRATION_NOT_CURRENT
            )
        finally:
            db.execute(
                # 6F 修正：恢复目标必须是当前 head（Phase 6A-6C 之后）
                text("UPDATE alembic_version SET version_num = '8c9d0e1f2a3b4'")
            )
            db.commit()
            assert check_pg_vector_readiness(db, dim) is None
            db.close()


class TestEmbeddingHealthOnPostgres:
    """Phase 5C：真实 PG 上的 /health/embedding 逻辑（check_embedding_health）。"""

    def test_ready_on_real_pg(self, pg_env):
        """迁移到 head 后：extension/表/维度齐备 → ready（verified 恒 False）。"""
        from app.services.embedding_health import check_embedding_health

        engine, SessionLocal = pg_env
        db = SessionLocal()
        try:
            payload = check_embedding_health(db)
            assert payload["status"] == "ready"
            assert payload["verified"] is False
            assert payload["semantic"] is False  # local-hash 明确非语义
        finally:
            db.close()

    def test_dimension_mismatch_on_real_pg(self, pg_env, monkeypatch):
        """配置维度与 VECTOR(n) 列不一致 → vector_dimension_mismatch（不降级）。"""
        from app.core.config import settings
        from app.services.embedding_health import check_embedding_health

        engine, SessionLocal = pg_env
        db = SessionLocal()
        monkeypatch.setattr(settings, "embedding_dimension", 512)
        try:
            payload = check_embedding_health(db)
            assert payload["status"] == "vector_dimension_mismatch"
        finally:
            db.close()
