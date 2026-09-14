"""检索索引服务测试（Phase 4B）：状态机、幂等、更新、故障回滚、级联删除。"""

import json

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base, _set_sqlite_pragma
from app.embeddings.local_hash_provider import LocalHashEmbeddingProvider
from app.models.document import (
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentIndexStatus,
    DocumentStatus,
)
from app.services import document_service, fts5
from app.services.document_errors import DocumentNotFoundError
from app.services.retrieval_errors import (
    DocumentNotReadyError,
    IndexingFailedError,
)
from app.services.retrieval_index_service import RetrievalIndexService, _content_sha256

TEXT_CN = "JARVIS 文档检索基础测试。第一段：记录任务与提醒。\n第二段：验证切块与索引。\n"
TEXT_EN = "JARVIS retrieval foundation test. First paragraph about reminders.\nSecond about chunking and indexing.\n"


@pytest.fixture()
def db_session(tmp_path):
    db_path = tmp_path / "idx.db"
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
    provider = LocalHashEmbeddingProvider(dimension=384)
    return RetrievalIndexService(db_session, provider=provider)


def _upload_ready(db: Session, content: bytes, name: str = "note.txt") -> Document:
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


def _fts_count(db: Session, document_id: int) -> int:
    return db.execute(
        text(
            f"SELECT COUNT(*) FROM {fts5.FTS_TABLE} WHERE document_id = :d"
        ),
        {"d": document_id},
    ).scalar()


# ---------- 拒绝路径 ----------


def test_missing_document_raises_not_found(db_session, service):
    with pytest.raises(DocumentNotFoundError):
        service.index_document(999999)


def test_non_ready_documents_rejected(db_session, service, _isolated_document_storage):
    # 上传失败文档（损坏 pdf）
    doc = document_service.upload_and_process(
        db_session,
        filename="bad.pdf",
        content_type="application/pdf",
        content=b"%PDF- broken content",
    )
    assert doc.status == DocumentStatus.FAILED.value
    with pytest.raises(DocumentNotReadyError):
        service.index_document(doc.id)


# ---------- 首次索引 ----------


def test_first_index_creates_embeddings_and_fts(db_session, service):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
    chunks = _chunks(db_session, doc.id)
    assert chunks

    stats = service.index_document(doc.id)
    db_session.refresh(doc)

    assert doc.index_status == DocumentIndexStatus.INDEXED.value
    assert doc.index_error_message is None
    assert stats.embeddings_created == len(chunks)
    assert stats.embeddings_skipped == 0
    assert stats.fts_rows_written == len(chunks)
    assert _fts_count(db_session, doc.id) == len(chunks)

    rows = (
        db_session.query(ChunkEmbedding)
        .filter(ChunkEmbedding.chunk_id.in_([c.id for c in chunks]))
        .all()
    )
    assert len(rows) == len(chunks)
    for row, chunk in zip(rows, chunks):
        assert row.chunk_id == chunk.id
        # Phase 5C：稳定 provider 标识（不再是类名）
        assert row.provider == "local-hash"
        assert row.model == "local-hash-v1"
        assert row.dimension == 384
        assert row.content_sha256 == _content_sha256(chunk.content)
        vector = json.loads(row.vector_json)
        assert isinstance(vector, list) and len(vector) == 384
        assert all(isinstance(v, float) and abs(v) <= 1.0 for v in vector)


# ---------- 幂等与更新 ----------


def test_repeat_index_is_idempotent_skip(db_session, service):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
    first = service.index_document(doc.id)
    second = service.index_document(doc.id)
    assert second.embeddings_skipped == first.embeddings_created
    assert second.embeddings_created == 0
    assert second.embeddings_updated == 0
    count = db_session.query(ChunkEmbedding).count()
    assert count == first.embeddings_created


def test_content_change_triggers_update(db_session, service):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
    service.index_document(doc.id)
    chunks = _chunks(db_session, doc.id)

    # 修改第一个 chunk 内容（模拟文档重新摄取）
    chunk = chunks[0]
    chunk.content = chunk.content + "（追加修改内容）"
    db_session.commit()

    stats = service.index_document(doc.id)
    assert stats.embeddings_updated == 1
    assert stats.embeddings_skipped == len(chunks) - 1
    row = (
        db_session.query(ChunkEmbedding)
        .filter(ChunkEmbedding.chunk_id == chunk.id)
        .one()
    )
    assert row.content_sha256 == _content_sha256(chunk.content)


def test_provider_change_triggers_update(db_session):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
    service_a = RetrievalIndexService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    service_a.index_document(doc.id)

    # 维度变化（模拟未来 provider 更换）：身份过期 → stale 替换（Phase 5C），
    # 向量必须重算且维度落库为 512
    service_b = RetrievalIndexService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=512)
    )
    stats = service_b.index_document(doc.id)
    assert stats.embeddings_stale > 0
    assert stats.embeddings_updated == 0
    assert stats.embeddings_created == 0
    row = db_session.query(ChunkEmbedding).first()
    assert row.dimension == 512
    assert row.provider == "local-hash"


def test_reindex_forces_full_rebuild(db_session, service):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
    service.index_document(doc.id)
    stats = service.reindex_document(doc.id)
    assert stats.embeddings_created == stats.chunks_scanned
    assert stats.embeddings_skipped == 0
    # 无重复行
    total = db_session.query(ChunkEmbedding).count()
    assert total == stats.chunks_scanned


# ---------- 故障注入与回滚 ----------


def test_embedding_failure_rolls_back_and_marks_index_failed(
    db_session, service, monkeypatch
):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
    chunks = _chunks(db_session, doc.id)
    assert chunks

    def boom(provider, contents):  # noqa: ARG001
        raise RuntimeError("simulated embedding failure")

    monkeypatch.setattr(
        "app.services.retrieval_index_service._embed_chunk_batch", boom
    )
    with pytest.raises(IndexingFailedError) as exc_info:
        service.index_document(doc.id)

    db_session.refresh(doc)
    assert doc.index_status == DocumentIndexStatus.INDEX_FAILED.value
    assert doc.status == DocumentStatus.READY.value  # 摄取状态不受影响
    assert doc.index_error_message == "indexing failed"
    assert exc_info.value.code == "INDEXING_FAILED"
    # 回滚：无半套数据
    assert db_session.query(ChunkEmbedding).count() == 0
    assert _fts_count(db_session, doc.id) == 0


def test_index_failed_document_can_be_retried(db_session, service, monkeypatch):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))

    def boom(provider, contents):  # noqa: ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "app.services.retrieval_index_service._embed_chunk_batch", boom
    )
    with pytest.raises(IndexingFailedError):
        service.index_document(doc.id)
    assert doc.index_status == DocumentIndexStatus.INDEX_FAILED.value

    monkeypatch.undo()
    stats = service.index_document(doc.id)
    assert stats.embeddings_created == stats.chunks_scanned
    db_session.refresh(doc)
    assert doc.index_status == DocumentIndexStatus.INDEXED.value
    assert doc.index_error_message is None


# ---------- 删除级联 ----------


def test_delete_document_cascades_embeddings_and_fts(db_session, service, _isolated_document_storage):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
    service.index_document(doc.id)
    assert _fts_count(db_session, doc.id) > 0

    document_service.delete_document(db_session, doc.id)

    assert db_session.query(ChunkEmbedding).count() == 0
    assert _fts_count(db_session, doc.id) == 0  # 无幽灵记录


def test_delete_document_index_is_idempotent(db_session, service):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
    service.delete_document_index(doc.id)  # 未索引：无操作
    service.index_document(doc.id)
    service.delete_document_index(doc.id)
    db_session.refresh(doc)
    assert doc.index_status == DocumentIndexStatus.NOT_INDEXED.value
    assert db_session.query(ChunkEmbedding).count() == 0
    assert _fts_count(db_session, doc.id) == 0
    service.delete_document_index(doc.id)  # 幂等


# ---------- 批量 ----------


def test_index_ready_documents_batch(db_session, service, monkeypatch):
    ready = _upload_ready(db_session, TEXT_CN.encode("utf-8"))
    other = _upload_ready(db_session, TEXT_EN.encode("utf-8"))
    service.index_document(ready.id)

    stats = service.index_ready_documents()
    # 只处理未索引的 other；ready 已 INDEXED 不在待处理列表
    assert stats.documents_scanned == 1
    assert stats.embeddings_created == len(_chunks(db_session, other.id))
    assert stats.failures == 0
    # 全量再跑：没有待处理
    again = service.index_ready_documents()
    assert again.documents_scanned == 0


def test_index_ready_documents_counts_failures(db_session, service, monkeypatch):
    doc = _upload_ready(db_session, TEXT_CN.encode("utf-8"))

    def boom(provider, contents):  # noqa: ARG001
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "app.services.retrieval_index_service._embed_chunk_batch", boom
    )
    stats = service.index_ready_documents()
    assert stats.documents_scanned == 1
    assert stats.failures == 1
    db_session.refresh(doc)
    assert doc.index_status == DocumentIndexStatus.INDEX_FAILED.value
