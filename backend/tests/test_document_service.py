"""DocumentService 测试：状态机、事务边界、故障注入、磁盘一致性。"""

import hashlib
import io

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base, _set_sqlite_pragma
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.services import document_service
from app.services.document_errors import (
    DocumentNotFoundError,
    InvalidDocumentStateError,
)


@pytest.fixture()
def db_session(tmp_path):
    db_path = tmp_path / "svc.db"
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


def _upload(db_session, content: bytes, filename: str = "note.txt") -> Document:
    return document_service.upload_and_process(
        db_session, filename=filename, content_type="text/plain", content=content
    )


# ---------- 成功路径 ----------


def test_upload_ready_document_with_chunks(db_session, _isolated_document_storage):
    text = "这是第一句话。这是第二句话。" * 40
    document = _upload(db_session, text.encode("utf-8"))
    assert document.status == DocumentStatus.READY.value
    assert document.error_code is None
    assert document.error_message is None
    assert document.sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
    # storage_key 为 32 位 hex（UUID），无扩展名
    assert len(document.storage_key) == 32
    assert document.storage_key.isalnum() and document.storage_key.islower()
    # 磁盘文件存在且内容一致
    stored = _isolated_document_storage / document.storage_key
    assert stored.read_bytes() == text.encode("utf-8")
    # chunks 落库
    chunks = (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document.id)
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )
    assert len(chunks) >= 2
    assert chunks[0].chunk_index == 0
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_same_content_uploads_are_independent_documents(db_session):
    """明确不做内容去重：相同 sha256 → 两个独立 Document（策略见 README）。"""
    first = _upload(db_session, b"same content")
    second = _upload(db_session, b"same content")
    assert first.sha256 == second.sha256
    assert first.id != second.id
    assert first.storage_key != second.storage_key


def test_list_documents_newest_first(db_session):
    first = _upload(db_session, b"first")
    second = _upload(db_session, b"second")
    documents = document_service.list_documents(db_session)
    assert [d.id for d in documents][:2] == [second.id, first.id]


def test_get_document_and_delete(db_session, _isolated_document_storage):
    document = _upload(db_session, b"to delete")
    assert document_service.get_document(db_session, document.id).id == document.id
    document_service.delete_document(db_session, document.id)
    assert db_session.get(Document, document.id) is None
    assert not (_isolated_document_storage / document.storage_key).exists()
    with pytest.raises(DocumentNotFoundError):
        document_service.get_document(db_session, document.id)


def test_delete_cascades_chunks(db_session):
    document = _upload(db_session, ("句子。" * 500).encode("utf-8"))
    assert (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document.id)
        .count()
        > 0
    )
    document_service.delete_document(db_session, document.id)
    assert (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document.id)
        .count()
        == 0
    )


def test_chunks_only_for_ready_documents(db_session):
    # 无 READY 文档直接查 chunks → 409 语义（InvalidDocumentStateError）
    with pytest.raises(DocumentNotFoundError):
        document_service.list_chunks(db_session, 999)
    document = _upload(db_session, b"x")
    chunks = document_service.list_chunks(db_session, document.id)
    assert isinstance(chunks, list)


# ---------- 故障注入 ----------


def test_db_fail_after_disk_write_cleans_up_file(db_session, _isolated_document_storage, monkeypatch):
    """注入首次 commit 失败：磁盘文件被清理，异常传播，不留孤儿文件。"""
    real_commit = Session.commit
    calls = {"n": 0}

    def flaky_commit(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SQLAlchemyError("simulated db failure")
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", flaky_commit)
    with pytest.raises(SQLAlchemyError):
        _upload(db_session, b"content that never lands")
    # 磁盘无任何残留文件（临时与目标都没有）
    assert list(_isolated_document_storage.iterdir()) == []
    assert db_session.query(Document).count() == 0


def test_parsing_failure_marks_document_failed(db_session, _isolated_document_storage):
    """加密 PDF：解析失败 → FAILED + 稳定错误码 + sanitized 简述。"""
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    encrypted = buf.getvalue()

    document = document_service.upload_and_process(
        db_session, filename="secret.pdf", content_type="application/pdf", content=encrypted
    )
    assert document.status == DocumentStatus.FAILED.value
    assert document.error_code == "DOCUMENT_PROCESSING_FAILED"
    assert "encrypted" in (document.error_message or "").lower()
    # 磁盘文件保留（供 DELETE 清理与审计）
    assert (_isolated_document_storage / document.storage_key).exists()
    # 无 chunks 落库
    assert (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document.id)
        .count()
        == 0
    )
    # FAILED 文档查 chunks → 409
    with pytest.raises(InvalidDocumentStateError):
        document_service.list_chunks(db_session, document.id)


def test_chunk_insert_failure_marks_document_failed(db_session, monkeypatch):
    """注入块写入失败：事务回滚后置 FAILED，不残留半截 chunks。"""
    def boom(db, document_id, chunks):
        raise SQLAlchemyError("simulated chunk insert failure")

    monkeypatch.setattr(document_service, "_insert_chunks", boom)
    document = _upload(db_session, ("句子。" * 300).encode("utf-8"))
    assert document.status == DocumentStatus.FAILED.value
    assert document.error_code == "DOCUMENT_PROCESSING_FAILED"
    assert (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document.id)
        .count()
        == 0
    )


def test_empty_text_yields_ready_document_with_no_chunks(db_session):
    document = _upload(db_session, b"")
    assert document.status == DocumentStatus.READY.value
    assert document.size_bytes == 0
    assert (
        db_session.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document.id)
        .count()
        == 0
    )


def test_upload_rejects_oversized_file_before_disk_write(db_session, _isolated_document_storage, monkeypatch):
    from app.services.document_errors import FileTooLargeError

    monkeypatch.setattr(document_service.settings, "document_max_file_size_mb", 1)
    with pytest.raises(FileTooLargeError):
        _upload(db_session, b"x" * (1024 * 1024 + 1))
    # 校验失败发生在落盘之前：存储目录不存在或为空
    storage = _isolated_document_storage
    assert not storage.exists() or list(storage.iterdir()) == []
    assert db_session.query(Document).count() == 0
