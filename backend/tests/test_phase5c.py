"""Phase 5C 测试：Semantic Embedding Production Readiness。

覆盖指令十一的 24 项要求（按主题组织为 10 个类）：
A. factory/provider 协议（provider_name / max_batch_size / capabilities / 未知拒绝）
B. 配置 fail-closed（fake 生产禁用、未知 provider 拒绝）
C. openai-compatible mock 传输（base_url / 有限重试 / 响应严格验证 /
   401/403/429/5xx/timeout/非法 JSON/数量/索引/NaN/Infinity/维度）
D. 批量与事务原子性（分批上限 / 单文档原子回滚 / sanitized 错误）
E. stale 状态机（created/updated/skipped/stale / force / 孤儿清理）
F. 零向量统一拒绝（服务层前置，SQLite 路径）
G. 健康 7 态（ready / config_invalid / key_missing / extension / schema /
   dimension / stale / verified 恒 False / /health/embedding 端点 200/503）
H. 密钥与向量防泄露（错误不含 Key/URL/原文）
I. PG 查询不降级（CAST 保留、参数绑定）
J. API schema 向后兼容（IndexStats 旧字段保留、新字段追加）

安全约定（与全库一致）：零网络（openai 全部 client_factory mock）、
零真实 sleep（retry_sleeper 注入记录式替身）、不读写 .env、
不触碰真实 jarvis.db。
"""

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings, settings
from app.core.database import Base, _set_sqlite_pragma
from app.embeddings.base import EmbeddingBatchTooLargeError, EmbeddingError
from app.embeddings.factory import create_embedding_provider
from app.embeddings.fake_provider import DeterministicFakeEmbeddingProvider
from app.embeddings.local_hash_provider import LocalHashEmbeddingProvider
from app.embeddings.openai_provider import OpenAIEmbeddingProvider
from app.models.document import (
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentIndexStatus,
    DocumentStatus,
)
from app.models.task import utcnow
from app.services import document_service
from app.services.retrieval_errors import IndexingFailedError
from app.services.retrieval_index_service import (
    IndexStats,
    RetrievalIndexService,
)

TEXT_CN = "JARVIS Phase 5C 测试文本。第一段：记录任务与提醒。\n第二段：验证嵌入生产化。\n"
TEXT_CN_2 = "JARVIS Phase 5C 测试文本。第一段：内容已修改。\n第二段：验证更新语义。\n"


@pytest.fixture()
def db_session(tmp_path):
    db_path = tmp_path / "p5c.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def service(db_session):
    return RetrievalIndexService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )


def _upload_ready(db: Session, content: bytes, name: str = "p5c.txt") -> Document:
    doc = document_service.upload_and_process(
        db, filename=name, content_type="text/plain", content=content
    )
    assert doc.status == DocumentStatus.READY.value
    return doc


def _chunks(db: Session, document_id: int) -> list[DocumentChunk]:
    return (
        db.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )


# =====================================================================
# A. factory / provider 协议（provider_name / max_batch_size /
#    capabilities / 未知 provider 拒绝）
# =====================================================================


class TestProviderProtocol:
    def test_local_hash_identity_and_capabilities(self):
        provider = create_embedding_provider("local-hash")
        assert provider.provider_name == "local-hash"
        assert provider.capabilities()["semantic"] is False
        assert provider.capabilities()["network"] is False
        assert provider.capabilities()["deterministic"] is True

    def test_fake_identity_and_capabilities(self):
        provider = DeterministicFakeEmbeddingProvider(dimension=8)
        assert provider.provider_name == "fake"
        assert provider.capabilities()["semantic"] is False

    def test_openai_alias_yields_openai_compatible_identity(self):
        # 配置名 openai（别名）→ provider 稳定标识 openai-compatible
        with pytest.raises(EmbeddingError) as exc_info:
            create_embedding_provider("openai")  # 无 Key → 构造失败
        assert exc_info.value.code == "EMBEDDING_NOT_CONFIGURED"
        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small", api_key="sk-test", dimension=512
        )
        assert provider.provider_name == "openai-compatible"
        caps = provider.capabilities()
        assert caps["semantic"] is True
        assert caps["network"] is True
        assert caps["deterministic"] is False

    def test_unknown_provider_rejected(self):
        with pytest.raises(EmbeddingError) as exc_info:
            create_embedding_provider("bogus")
        assert exc_info.value.code == "UNKNOWN_EMBEDDING_PROVIDER"

    def test_provider_name_default_is_class_name(self):
        """基类默认 = 类名：直接继承 EmbeddingProvider 的子类不受破坏。"""
        from app.embeddings.base import EmbeddingProvider

        class MyProvider(EmbeddingProvider):
            @property
            def dimension(self) -> int:
                return 8

            @property
            def model_name(self) -> str:
                return "my-model"

            def _embed_batch(self, texts: list[str]) -> list[list[float]]:
                return [[1.0] * 8 for _ in texts]

        assert MyProvider().provider_name == "MyProvider"

    def test_max_batch_size_defaults_to_config(self):
        provider = LocalHashEmbeddingProvider(dimension=8)
        assert provider.max_batch_size == settings.embedding_batch_max

    def test_max_batch_size_enforced_by_provider(self):
        provider = LocalHashEmbeddingProvider(dimension=8)
        with pytest.raises(EmbeddingBatchTooLargeError):
            provider.embed_documents(["x"] * (provider.max_batch_size + 1))


# =====================================================================
# B. 配置 fail-closed（fake 生产禁用 / 未知 provider 拒绝）
# =====================================================================


class TestConfigFailClosed:
    def test_fake_provider_rejected_in_config(self):
        with pytest.raises(ValidationError):
            Settings(embedding_provider="fake")

    def test_unknown_provider_rejected_in_config(self):
        with pytest.raises(ValidationError):
            Settings(embedding_provider="bogus")

    def test_fake_usable_only_via_direct_construction(self):
        # 生产配置禁 fake；测试路径直接构造实例（文档声明路径）
        provider = DeterministicFakeEmbeddingProvider(dimension=8)
        vectors = provider.embed_documents(["a", "b"])
        assert len(vectors) == 2
        assert len(vectors[0]) == 8


# =====================================================================
# C. openai-compatible mock 传输（base_url / 有限重试 / 响应严格验证）
# =====================================================================


class _OkResponse:
    def __init__(self, vectors, indexes=None):
        self.data = [
            SimpleNamespace(
                index=(indexes[i] if indexes is not None else i),
                embedding=vectors[i],
            )
            for i in range(len(vectors))
        ]


class _RecordingSleeper:
    """记录式 sleeper：验证重试次数与退避（单元测试绝不真实 sleep）。"""

    def __init__(self):
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def _make_client(create_fn):
    class FakeClient:
        def __init__(self):
            self.embeddings = SimpleNamespace(create=create_fn)

    return FakeClient()


class TestOpenAiCompatTransport:
    def _provider(
        self, create_fn, *, max_retries=2, sleeper=None, **kwargs
    ) -> OpenAIEmbeddingProvider:
        return OpenAIEmbeddingProvider(
            dimension=512,
            model="text-embedding-3-small",
            api_key="sk-test",
            timeout_seconds=5.0,
            max_retries=max_retries,
            retry_backoff_seconds=0.5,
            retry_sleeper=sleeper or _RecordingSleeper(),
            client_factory=lambda: _make_client(create_fn),
            **kwargs,
        )

    def test_missing_key_rejected_at_construction(self):
        with pytest.raises(EmbeddingError) as exc_info:
            OpenAIEmbeddingProvider(
                model="text-embedding-3-small", api_key=""
            )
        assert exc_info.value.code == "EMBEDDING_NOT_CONFIGURED"

    def test_base_url_passed_to_client(self, monkeypatch):
        captured: dict[str, Any] = {}
        import sys

        class FakeOpenAI:
            def __init__(self, **kwargs):
                captured.update(kwargs)

        # _get_client 内 `import openai` 从 sys.modules 解析：
        # 注入 fake 模块（真实 openai 包是否安装均不影响测试）
        monkeypatch.setitem(
            sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI)
        )
        provider = OpenAIEmbeddingProvider(
            model="text-embedding-3-small",
            api_key="sk-test",
            dimension=512,
            base_url="https://embed.example.test/v1",
        )
        provider._get_client()
        assert captured["base_url"] == "https://embed.example.test/v1"
        assert captured["api_key"] == "sk-test"
        assert captured["timeout"] == settings.embedding_timeout_seconds

    def test_valid_response_with_out_of_order_indexes(self):
        # index 完整但乱序 → 确定性重排（结果与输入顺序严格一致）
        def create(**kwargs):
            return _OkResponse(
                [[0.0] * 512, [1.0] * 512, [0.5] * 512], indexes=[2, 0, 1]
            )

        provider = self._provider(create)
        vectors = provider.embed_documents(["second", "third", "first"])
        assert vectors[0] == [1.0] * 512
        assert vectors[1] == [0.5] * 512
        assert vectors[2] == [0.0] * 512

    def test_count_mismatch_rejected_without_retry(self):
        sleeper = _RecordingSleeper()

        def create(**kwargs):
            return _OkResponse([[0.0] * 512])  # 1 个，期望 2 个

        provider = self._provider(create, sleeper=sleeper)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_RESPONSE_INVALID"
        assert sleeper.calls == []  # 响应非法：绝不重试

    def test_missing_index_rejected(self):
        class BadResponse:
            def __init__(self):
                self.data = [SimpleNamespace(embedding=[0.0] * 512)]

        def create(**kwargs):
            return BadResponse()

        provider = self._provider(create)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_RESPONSE_INVALID"

    def test_unreadable_response_rejected(self):
        def create(**kwargs):
            return SimpleNamespace()  # 无 data

        provider = self._provider(create)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_RESPONSE_INVALID"

    def test_non_finite_values_rejected(self):
        def create(**kwargs):
            # 数量正确、index 正确 → 数值检查命中 NaN
            return _OkResponse([[float("nan")] * 512, [0.0] * 512])

        provider = self._provider(create)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_INTERNAL_ERROR"

    def test_infinity_values_rejected(self):
        def create(**kwargs):
            return _OkResponse([[float("inf")] * 512, [0.0] * 512])

        provider = self._provider(create)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_INTERNAL_ERROR"

    def test_dimension_mismatch_rejected(self):
        def create(**kwargs):
            return _OkResponse([[0.0] * 384, [0.0] * 384])  # 配置 512，返回 384

        provider = self._provider(create)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_INTERNAL_ERROR"

    def test_empty_vector_rejected(self):
        def create(**kwargs):
            return _OkResponse([[], []])  # 数量正确、维度为 0

        provider = self._provider(create)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_INTERNAL_ERROR"

    def test_retryable_rate_limit_retries_then_succeeds(self):
        sleeper = _RecordingSleeper()
        state = {"calls": 0}

        class RateLimitError(Exception):
            pass

        def create(**kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise RateLimitError()
            return _OkResponse([[1.0] * 512, [0.5] * 512])

        provider = self._provider(create, sleeper=sleeper)
        vectors = provider.embed_documents(["a", "b"])
        assert vectors[0] == [1.0] * 512
        assert vectors[1] == [0.5] * 512
        assert state["calls"] == 2
        assert sleeper.calls == [0.5]  # 首次退避 backoff * 2**0

    def test_rate_limit_retries_exhausted_raises(self):
        sleeper = _RecordingSleeper()

        class RateLimitError(Exception):
            pass

        def create(**kwargs):
            raise RateLimitError()

        provider = self._provider(create, max_retries=2, sleeper=sleeper)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_RATE_LIMITED"
        assert sleeper.calls == [0.5, 1.0]  # 指数退避：0.5, 0.5*2

    def test_timeout_retries(self):
        sleeper = _RecordingSleeper()
        state = {"calls": 0}

        class APITimeoutError(Exception):
            pass

        def create(**kwargs):
            state["calls"] += 1
            if state["calls"] < 3:
                raise APITimeoutError()
            return _OkResponse([[1.0] * 512, [0.5] * 512])

        provider = self._provider(create, max_retries=2, sleeper=sleeper)
        vectors = provider.embed_documents(["a", "b"])
        assert len(vectors) == 2
        assert state["calls"] == 3
        assert sleeper.calls == [0.5, 1.0]

    def test_5xx_retries(self):
        sleeper = _RecordingSleeper()

        class APIStatusError(Exception):
            def __init__(self, status_code):
                self.status_code = status_code

        state = {"calls": 0}

        def create(**kwargs):
            state["calls"] += 1
            if state["calls"] == 1:
                raise APIStatusError(500)
            return _OkResponse([[1.0] * 512, [0.5] * 512])

        provider = self._provider(create, sleeper=sleeper)
        vectors = provider.embed_documents(["a", "b"])
        assert len(vectors) == 2
        assert state["calls"] == 2
        assert sleeper.calls == [0.5]

    def test_401_rejected_without_retry(self):
        sleeper = _RecordingSleeper()

        class AuthenticationError(Exception):
            pass

        def create(**kwargs):
            raise AuthenticationError()

        provider = self._provider(create, max_retries=5, sleeper=sleeper)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_INVALID_KEY"
        assert sleeper.calls == []  # 401：绝不重试

    def test_403_rejected_without_retry(self):
        sleeper = _RecordingSleeper()

        class PermissionDeniedError(Exception):
            pass

        def create(**kwargs):
            raise PermissionDeniedError()

        provider = self._provider(create, max_retries=5, sleeper=sleeper)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_INVALID_KEY"
        assert sleeper.calls == []

    def test_other_4xx_rejected_without_retry(self):
        sleeper = _RecordingSleeper()

        class APIStatusError(Exception):
            def __init__(self, status_code):
                self.status_code = status_code

        def create(**kwargs):
            raise APIStatusError(404)

        provider = self._provider(create, max_retries=5, sleeper=sleeper)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert exc_info.value.code == "EMBEDDING_UPSTREAM_ERROR"
        assert sleeper.calls == []

    def test_max_retries_zero_means_no_retry(self):
        sleeper = _RecordingSleeper()

        class RateLimitError(Exception):
            pass

        def create(**kwargs):
            raise RateLimitError()

        provider = self._provider(create, max_retries=0, sleeper=sleeper)
        with pytest.raises(EmbeddingError):
            provider.embed_documents(["a", "b"])
        assert sleeper.calls == []

    def test_error_messages_do_not_leak_key_or_url(self):
        class AuthenticationError(Exception):
            pass

        def create(**kwargs):
            raise AuthenticationError()

        provider = self._provider(create)
        with pytest.raises(EmbeddingError) as exc_info:
            provider.embed_documents(["a", "b"])
        assert "sk-test" not in exc_info.value.message
        assert "embed.example" not in exc_info.value.message


# =====================================================================
# D. 批量与事务原子性（分批上限 / 单文档原子回滚 / sanitized 错误）
# =====================================================================


class TestBatchAndAtomicity:
    def test_embed_batch_splits_by_provider_max_batch_size(self):
        """provider 声明更小批量上限 → 服务层按它分批调用（外部限流合规）。"""
        calls: list[int] = []

        class FakeClient:
            def __init__(self):
                self.embeddings = SimpleNamespace(
                    create=lambda **kw: self._create(kw)
                )

            def _create(self, kwargs):
                calls.append(len(kwargs["input"]))
                return _OkResponse(
                    [[0.0] * 512 for _ in kwargs["input"]],
                    indexes=list(range(len(kwargs["input"]))),
                )

        class SmallBatchOpenAI(OpenAIEmbeddingProvider):
            @property
            def max_batch_size(self) -> int:
                return 2

        provider = SmallBatchOpenAI(
            dimension=512,
            model="text-embedding-3-small",
            api_key="sk-test",
            timeout_seconds=5.0,
            client_factory=lambda: FakeClient(),
        )
        from app.services.retrieval_index_service import _embed_chunk_batch

        vectors = _embed_chunk_batch(provider, ["a", "b", "c"])
        assert len(vectors) == 3
        assert calls == [2, 1]  # 2+1 分批

    def test_embedding_failure_rolls_back_transaction(self, db_session):
        """嵌入失败 → 整体回滚：无半套向量、INDEX_FAILED、sanitized 错误。"""
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        chunks = _chunks(db_session, doc.id)
        assert chunks

        class ExplodingProvider(LocalHashEmbeddingProvider):
            def _embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("upstream exploded (simulated)")

        service = RetrievalIndexService(db_session, provider=ExplodingProvider(384))
        with pytest.raises(IndexingFailedError) as exc_info:
            service.index_document(doc.id)
        db_session.refresh(doc)
        assert doc.index_status == DocumentIndexStatus.INDEX_FAILED.value
        # sanitized 错误：不泄露内部异常细节
        assert exc_info.value.message != "upstream exploded (simulated)"
        # 无半套向量：事务整体回滚
        leftover = (
            db_session.query(ChunkEmbedding)
            .filter(ChunkEmbedding.chunk_id.in_([c.id for c in chunks]))
            .count()
        )
        assert leftover == 0

    def test_index_ready_documents_counts_failures(self, db_session):
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))

        class ExplodingProvider(LocalHashEmbeddingProvider):
            def _embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError("boom")

        service = RetrievalIndexService(db_session, provider=ExplodingProvider(384))
        stats = service.index_ready_documents()
        assert stats.failures == 1
        db_session.refresh(doc)
        assert doc.index_status == DocumentIndexStatus.INDEX_FAILED.value


# =====================================================================
# E. stale 状态机（created/updated/skipped/stale / force / 孤儿清理）
# =====================================================================


class TestStaleStateMachine:
    def test_created_then_skipped(self, db_session, service):
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        chunks = _chunks(db_session, doc.id)
        first = service.index_document(doc.id)
        assert first.embeddings_created == len(chunks)
        assert first.embeddings_skipped == 0
        second = service.index_document(doc.id)
        assert second.embeddings_skipped == len(chunks)
        assert second.embeddings_created == 0
        assert second.embeddings_updated == 0
        assert second.embeddings_stale == 0

    def test_content_change_triggers_updated(self, db_session):
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        service = RetrievalIndexService(
            db_session, provider=LocalHashEmbeddingProvider(384)
        )
        service.index_document(doc.id)
        chunks = _chunks(db_session, doc.id)
        # 修改 chunk 内容（模拟文档重新摄取）→ 身份未变、内容变化 → updated
        chunk = chunks[0]
        chunk.content = chunk.content + "（追加修改内容）"
        db_session.commit()
        stats = service.index_document(doc.id)
        assert stats.embeddings_updated == 1
        assert stats.embeddings_stale == 0

    def test_identity_change_triggers_stale(self, db_session):
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        RetrievalIndexService(
            db_session, provider=DeterministicFakeEmbeddingProvider(dimension=384)
        ).index_document(doc.id)
        # 换回 local-hash（身份不同）→ stale 替换
        stats = RetrievalIndexService(
            db_session, provider=LocalHashEmbeddingProvider(384)
        ).index_document(doc.id)
        assert stats.embeddings_stale > 0
        assert stats.embeddings_updated == 0
        assert stats.embeddings_created == 0
        row = db_session.query(ChunkEmbedding).first()
        assert row.provider == "local-hash"

    def test_force_reindex_counts_created(self, db_session, service):
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        chunks = _chunks(db_session, doc.id)
        service.index_document(doc.id)
        stats = service.reindex_document(doc.id)
        assert stats.embeddings_created == len(chunks)
        assert stats.embeddings_stale == 0
        assert stats.embeddings_updated == 0

    def test_orphan_rows_cleaned_and_counted(self, tmp_path):
        """孤儿 = 向量行存在但 chunk 已不存在（无 FK 约束库模拟历史残留）。"""
        db_path = tmp_path / "nofk.db"
        engine = create_engine(
            f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
        )
        # 注意：不挂 _set_sqlite_pragma —— 模拟 FK 未启用的历史库，
        # 删除 chunk 后向量行残留（孤儿产生路径）
        Base.metadata.create_all(bind=engine)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        db = SessionLocal()
        try:
            doc = _upload_ready(db, TEXT_CN.encode("utf-8"))
            service = RetrievalIndexService(
                db, provider=LocalHashEmbeddingProvider(384)
            )
            service.index_document(doc.id)
            chunks = _chunks(db, doc.id)
            assert chunks
            # 删除全部 chunk：无 FK → 向量行残留 → 孤儿
            db.query(DocumentChunk).filter(
                DocumentChunk.document_id == doc.id
            ).delete()
            db.commit()
            leftover = db.query(ChunkEmbedding).count()
            assert leftover == len(chunks)  # 孤儿已产生

            # 重新摄取（新 chunks）→ 索引时全局孤儿清理
            doc2 = _upload_ready(
                db, TEXT_CN_2.encode("utf-8"), name="p5c2.txt"
            )
            stats = service.index_document(doc2.id)
            assert stats.deleted_orphans == leftover
            assert db.query(ChunkEmbedding).count() == len(
                _chunks(db, doc2.id)
            )  # 孤儿清空，仅剩合法行
        finally:
            db.close()
            Base.metadata.drop_all(bind=engine)

    def test_no_orphans_is_idempotent(self, db_session, service):
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        service.index_document(doc.id)
        stats = service.index_document(doc.id)
        assert stats.deleted_orphans == 0

    def test_vector_search_filters_stale_identity_rows(self, db_session):
        """搜索身份过滤（backend 层）：同维度身份过期的旧向量行不参与检索。

        模拟「旧 provider 已索引、切换 provider 后尚未 reindex」：
        旧身份行仍在库中，但带当前身份搜索 → 只返回当前身份向量。
        """
        from app.search.factory import get_search_backend

        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        RetrievalIndexService(
            db_session, provider=DeterministicFakeEmbeddingProvider(384)
        ).index_document(doc.id)
        backend = get_search_backend()
        query_vector = LocalHashEmbeddingProvider(384).embed_query("JARVIS")
        # 当前身份 = local-hash/local-hash-v1/384：无该身份的向量行 → 空结果
        candidates, total = backend.vector_search(
            db_session,
            query_vector=query_vector,
            document_ids=None,
            limit=5,
            identity=("local-hash", "local-hash-v1", 384),
        )
        assert candidates == []
        assert total == 0
        # 兼容路径（不传身份，None=不过滤）：旧行仍在 → 有结果
        candidates, total = backend.vector_search(
            db_session, query_vector=query_vector, document_ids=None, limit=5
        )
        assert len(candidates) > 0
        assert total > 0

    def test_search_service_excludes_stale_identity_rows(self, db_session):
        """搜索身份过滤（服务层端到端）：RetrievalService 必须传当前身份。

        生产搜索路径（keyword/vector/hybrid 均经由 _search_vector）只返回
        当前有效身份的向量行；旧身份行不参与排名。
        """
        from app.services.retrieval_service import RetrievalService

        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        RetrievalIndexService(
            db_session, provider=DeterministicFakeEmbeddingProvider(384)
        ).index_document(doc.id)
        service = RetrievalService(
            db_session, provider=LocalHashEmbeddingProvider(384)
        )
        result = service.search(query="JARVIS", mode="vector", top_k=5)
        assert result.items == []  # 旧 fake 身份行被过滤，无当前身份行
        assert result.embedding_model == "local-hash-v1"


# =====================================================================
# F. 零向量统一拒绝（服务层前置，SQLite 路径）
# =====================================================================


class TestZeroVectorRejection:
    def test_zero_vector_rolls_back_transaction(self, db_session):
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))

        class ZeroVectorProvider(LocalHashEmbeddingProvider):
            def _embed_batch(self, texts: list[str]) -> list[list[float]]:
                return [[0.0] * 384 for _ in texts]

        service = RetrievalIndexService(
            db_session, provider=ZeroVectorProvider(384)
        )
        with pytest.raises(IndexingFailedError):
            service.index_document(doc.id)
        db_session.refresh(doc)
        assert doc.index_status == DocumentIndexStatus.INDEX_FAILED.value
        assert db_session.query(ChunkEmbedding).count() == 0  # 无半套向量


# =====================================================================
# G. 健康 7 态（ready / config_invalid / key_missing / extension /
#    schema / dimension / stale / verified / /health/embedding）
# =====================================================================


class TestEmbeddingHealth:
    def _check(self, db):
        from app.services.embedding_health import check_embedding_health

        return check_embedding_health(db)

    def test_sqlite_ready(self, db_session):
        payload = self._check(db_session)
        assert payload["status"] == "ready"
        assert payload["provider"] == "local-hash"
        assert payload["semantic"] is False
        assert payload["verified"] is False  # 未在线 smoke 不得 verified=true

    def test_openai_key_missing(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "embedding_provider", "openai")
        monkeypatch.setattr(settings, "embedding_api_key", "")
        payload = self._check(db_session)
        assert payload["status"] == "provider_key_missing"

    def test_provider_config_invalid(self, db_session, monkeypatch):
        monkeypatch.setattr(settings, "embedding_provider", "openai")
        monkeypatch.setattr(settings, "embedding_api_key", "sk-test")
        monkeypatch.setattr(settings, "embedding_model", "unknown-model")
        payload = self._check(db_session)
        assert payload["status"] == "provider_config_invalid"
        assert payload["error_code"] == "EMBEDDING_UNKNOWN_MODEL"

    def test_vector_extension_missing(self, db_session, monkeypatch):
        monkeypatch.setattr(
            "app.services.embedding_health.check_pg_vector_readiness",
            lambda db, dim: "VECTOR_EXTENSION_MISSING",
        )
        payload = self._check(_PGStub())
        assert payload["status"] == "vector_extension_missing"

    def test_vector_schema_missing(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.embedding_health.check_pg_vector_readiness",
            lambda db, dim: "VECTOR_SCHEMA_MISSING",
        )
        payload = self._check(_PGStub())
        assert payload["status"] == "vector_schema_missing"

    def test_vector_dimension_mismatch(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.embedding_health.check_pg_vector_readiness",
            lambda db, dim: "VECTOR_DIMENSION_MISMATCH",
        )
        payload = self._check(_PGStub())
        assert payload["status"] == "vector_dimension_mismatch"

    def test_database_unavailable_reported_honestly(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.embedding_health.check_pg_vector_readiness",
            lambda db, dim: "DB_UNAVAILABLE",
        )
        payload = self._check(_PGStub())
        assert payload["status"] == "database_unavailable"

    def test_index_stale(self, db_session):
        # 用 fake 身份写入向量行 → 默认 local-hash 下身份过期
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        RetrievalIndexService(
            db_session, provider=DeterministicFakeEmbeddingProvider(384)
        ).index_document(doc.id)
        payload = self._check(db_session)
        assert payload["status"] == "embedding_index_stale"

    def test_health_embedding_endpoint_ready(self, client):
        response = client.get("/health/embedding")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["verified"] is False

    def test_health_embedding_endpoint_fails_closed(self, client, monkeypatch):
        monkeypatch.setattr(settings, "embedding_provider", "openai")
        monkeypatch.setattr(settings, "embedding_api_key", "")
        response = client.get("/health/embedding")
        assert response.status_code == 503
        assert response.json()["status"] == "provider_key_missing"


class _PGStub:
    """方言为 PostgreSQL 的测试桩（配合 monkeypatch 的 readiness 检查）。"""

    def __init__(self):
        self.bind = SimpleNamespace(
            dialect=SimpleNamespace(name="postgresql")
        )


# =====================================================================
# H. 密钥与向量防泄露（错误不含 Key/URL/原文；API 响应不含向量）
# =====================================================================


class TestLeakageProtection:
    def test_index_stats_contains_no_vectors_or_keys(self, db_session, service):
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
        stats = service.index_document(doc.id).to_dict()
        serialized = json.dumps(stats)
        assert "sk-" not in serialized  # 无密钥
        assert all(isinstance(v, int) for v in stats.values())  # 全部整数计数
        # 无向量值（向量是浮点列表，绝不进入统计）
        assert not any(isinstance(v, list) for v in stats.values())

    def test_indexing_error_message_is_sanitized(self, db_session):
        doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))

        class NoisyProvider(LocalHashEmbeddingProvider):
            def _embed_batch(self, texts: list[str]) -> list[list[float]]:
                raise RuntimeError(
                    "connection refused to https://internal.example:9999 "
                    "with key sk-super-secret"
                )

        service = RetrievalIndexService(db_session, provider=NoisyProvider(384))
        with pytest.raises(IndexingFailedError) as exc_info:
            service.index_document(doc.id)
        # IndexingFailedError.message 是固定包装文案；reason 为 sanitized 详情
        assert exc_info.value.reason == "indexing failed"
        assert "sk-super-secret" not in exc_info.value.reason
        assert "internal.example" not in exc_info.value.reason
        # 文档持久化的 index_error_message 也是 sanitized
        db_session.refresh(doc)
        assert doc.index_error_message == "indexing failed"


# =====================================================================
# I. PG 查询不降级（CAST 保留、参数绑定）
# =====================================================================


class TestPgQueryNotDegraded:
    def test_vector_sql_keeps_explicit_cast(self):
        from app.search.postgresql_backend import build_vector_sql

        sql, params = build_vector_sql(None, 5)
        assert "CAST(:q AS vector)" in sql  # psycopg3 list → vector 显式转换
        assert "<=>" in sql
        assert params["limit"] == 5

    def test_vector_sql_binds_parameters_never_inlines(self):
        from app.search.postgresql_backend import build_vector_sql

        sql, _params = build_vector_sql([1, 2, 3], 5)
        # 文档 ID 全部参数绑定：SQL 文本中绝不出现内联 ID 列表
        assert ":doc_0" in sql and ":doc_1" in sql and ":doc_2" in sql
        assert ":q" in sql
        assert "IN (1, 2, 3)" not in sql
        assert "1,2,3" not in sql

    def test_vector_sql_filters_stale_identity_rows(self):
        """PG 搜索身份过滤（离线 SQL 构造断言，不依赖真实 PG）。"""
        from app.search.postgresql_backend import build_vector_sql

        sql, params = build_vector_sql(
            None, 5, identity=("local-hash", "local-hash-v1", 384)
        )
        # 身份条件全部参数绑定：provider/model_name/dimension
        assert "pv.provider = :vp" in sql
        assert "pv.model_name = :vm" in sql
        assert "pv.dimension = :vd" in sql
        assert params["vp"] == "local-hash"
        assert params["vm"] == "local-hash-v1"
        assert params["vd"] == 384
        # CAST 保留（不降级）与身份条件共存
        assert "CAST(:q AS vector)" in sql


# =====================================================================
# J. API schema 向后兼容（IndexStats 旧字段保留、新字段追加）
# =====================================================================


class TestApiSchemaBackwardCompat:
    def test_index_stats_keeps_legacy_fields(self):
        legacy = {
            "documents_scanned",
            "chunks_scanned",
            "embeddings_created",
            "embeddings_updated",
            "embeddings_skipped",
            "fts_rows_written",
            "failures",
        }
        new_fields = {"embeddings_stale", "deleted_orphans"}
        assert legacy <= set(IndexStats().to_dict())
        assert new_fields <= set(IndexStats().to_dict())

    def test_index_api_response_has_both_legacy_and_new_fields(
        self, client
    ):
        # 上传 → READY → 索引 → 响应包含旧字段与新字段
        import io

        response = client.post(
            "/api/documents",
            files={"file": ("p5c.txt", io.BytesIO(TEXT_CN.encode("utf-8")))},
        )
        assert response.status_code == 201, response.text
        doc_id = response.json()["id"]
        index_response = client.post(f"/api/retrieval/index/{doc_id}")
        assert index_response.status_code == 200, index_response.text
        body = index_response.json()
        for field in (
            "embeddings_created",
            "embeddings_updated",
            "embeddings_skipped",
            "embeddings_stale",
            "deleted_orphans",
            "fts_rows_written",
        ):
            assert field in body
