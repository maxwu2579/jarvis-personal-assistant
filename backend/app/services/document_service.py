"""文档摄取服务（Phase 4A）：上传校验 → 原子落盘 → 解析 → 切块 → 状态机。

事务边界（也是可测试的故障注入点）：
1. 上传事务：校验（扩展名/Content-Type/大小/文件头/ZIP bomb）→ sha256 →
   原子落盘 → DB 插入 UPLOADED 并 commit。DB 失败 → 回滚 + 清理磁盘文件，
   不留「文件在、库无行」的孤儿。
2. 处理事务 A：UPLOADED -> PROCESSING（独立 commit，状态可观测）。
3. 解析阶段：ParsingError → 事务 B：PROCESSING -> FAILED + 稳定错误码
   （sanitized 简述；磁盘文件保留，供 DELETE 清理，不实现重试端点）。
4. 处理事务 C：删除旧 chunks + 插入新 chunks + PROCESSING -> READY
   （单事务，块序列原子可见）。失败 → 回滚 → 事务 B（FAILED）。

磁盘文件与 DB 行的一致性：任何路径都不会出现「DB 有行、磁盘无文件」
（上传先落盘后入库；FAILED 保留文件）。删除路径先删 DB 行（成功）再删
磁盘文件，文件删除失败仅告警（可能留下不可达的孤儿文件，不影响请求）。

不在本文件读取 HTTP/UploadFile：service 只接收原始字节与元信息。
"""

import hashlib
import logging
import uuid

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document, DocumentChunk, DocumentStatus
from app.search.factory import get_search_backend
from app.services import document_storage
from app.services.chunking import chunk_text
from app.services.document_errors import (
    DocumentNotFoundError,
    DocumentProcessingError,
    FileTooLargeError,
    InvalidDocumentStateError,
)
from app.services.parsers import (
    ParsingError,
    check_content_type,
    check_docx_zip_bomb,
    check_extension,
    check_file_header,
    get_parser,
)

logger = logging.getLogger(__name__)

# 最大文件大小（字节）按配置换算，上传前就拒绝超限请求
MAX_FILENAME_LENGTH = 255


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _insert_chunks(db: Session, document_id: int, chunks: list[dict]) -> None:
    """插入块序列（模块级函数：测试可 monkeypatch 注入失败）。"""
    for index, chunk in enumerate(chunks):
        db.add(
            DocumentChunk(
                document_id=document_id,
                chunk_index=index,
                content=chunk["content"],
                char_start=chunk["char_start"],
                char_end=chunk["char_end"],
                token_estimate=chunk["token_estimate"],
            )
        )


def _mark_failed(db: Session, document: Document, error: DocumentProcessingError) -> Document:
    """把文档置为 FAILED 并记录 sanitized 错误（独立事务）。"""
    db.rollback()  # 清理可能失败的旧事务，确保行可写
    document.status = DocumentStatus.FAILED.value
    document.error_code = error.code
    document.error_message = error.message[:500]
    db.commit()
    db.refresh(document)
    return document


def _check_upload_limits(size_bytes: int) -> None:
    max_bytes = settings.document_max_file_size_mb * 1024 * 1024
    if size_bytes > max_bytes:
        raise FileTooLargeError(
            f"file is too large ({size_bytes} bytes > {max_bytes} bytes)"
        )


def _validate_upload(filename: str, content_type: str, content: bytes) -> str:
    """上传阶段校验（任何失败不落库、不落盘）。返回小写扩展名。"""
    ext = check_extension(filename)
    check_content_type(content_type, ext)
    _check_upload_limits(len(content))
    check_file_header(content, ext)
    if ext == ".docx":
        check_docx_zip_bomb(content)
    return ext


def upload_and_process(
    db: Session,
    *,
    filename: str,
    content_type: str,
    content: bytes,
) -> Document:
    """完整摄取管线：校验 → 落盘 → UPLOADED → PROCESSING → READY | FAILED。

    返回最终文档（READY 或 FAILED）。上传阶段校验失败抛 DocumentError
    （由 API 层映射为 4xx）；处理阶段失败返回 status=FAILED 的文档。
    """
    ext = _validate_upload(filename, content_type, content)

    # ---- 1. 上传事务：原子落盘 → DB 插入 UPLOADED ----
    storage_key = uuid.uuid4().hex
    storage_dir = document_storage.ensure_storage_dir(settings.document_storage_dir)
    document_storage.save_upload_atomic(storage_dir, storage_key, content)
    document = Document(
        original_filename=filename[:MAX_FILENAME_LENGTH],
        content_type=(content_type or "application/octet-stream").split(";")[0].strip(),
        size_bytes=len(content),
        sha256=_sha256(content),
        storage_key=storage_key,
        status=DocumentStatus.UPLOADED.value,
    )
    try:
        db.add(document)
        db.commit()
        db.refresh(document)
    except Exception:
        db.rollback()
        # DB-fail cleanup：文件已落盘但库无行 → 清理磁盘，不留孤儿
        document_storage.delete_file(storage_dir, storage_key)
        raise

    # ---- 2. 处理事务 A：UPLOADED -> PROCESSING ----
    document.status = DocumentStatus.PROCESSING.value
    db.commit()
    db.refresh(document)

    # ---- 3. 解析（失败 → FAILED，保留文件供审计/删除） ----
    try:
        text = get_parser(ext).extract_text(content)
    except ParsingError as exc:
        # ParsingError 文案由 parsers 内部固定构造（sanitized），可安全入库
        logger.warning(
            "document.parse_failed id=%s status=%s", document.id, document.status
        )
        return _mark_failed(
            db, document, DocumentProcessingError(str(exc) or "text extraction failed")
        )
    except Exception:
        # 解析器内部的意外异常：同样落到 FAILED，绝不 500 崩溃上传请求。
        # 意外异常的原始消息可能敏感，使用固定文案。
        logger.exception("document.parse_unexpected id=%s", document.id)
        return _mark_failed(
            db, document, DocumentProcessingError("text extraction failed")
        )

    # ---- 4. 处理事务 C：切块 + 入库 + READY（单事务原子可见） ----
    try:
        chunks = chunk_text(
            text,
            chunk_size=settings.document_chunk_size_chars,
            overlap=settings.document_chunk_overlap_chars,
        )
        db.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == document.id)
        )  # 幂等：重试不会产生重复块
        _insert_chunks(db, document.id, chunks)
        document.status = DocumentStatus.READY.value
        db.commit()
        db.refresh(document)
    except Exception:
        logger.exception("document.chunking_failed id=%s", document.id)
        return _mark_failed(db, document, DocumentProcessingError("chunking failed"))
    return document


def list_documents(db: Session) -> list[Document]:
    return db.query(Document).order_by(Document.created_at.desc(), Document.id.desc()).all()


def indexed_chunk_counts(db: Session, document_ids: list[int]) -> dict[int, int]:
    """每个文档的已索引块数（方言无关，经 SearchBackend 单聚合查询）。"""
    return get_search_backend().count_vector_rows(db, document_ids)


def get_document(db: Session, document_id: int) -> Document:
    document = db.get(Document, document_id)
    if document is None:
        raise DocumentNotFoundError(document_id)
    return document


def list_chunks(db: Session, document_id: int) -> list[DocumentChunk]:
    """返回 READY 文档的块序列（按 chunk_index 升序）。非 READY → 409。"""
    document = get_document(db, document_id)
    if DocumentStatus(document.status) is not DocumentStatus.READY:
        raise InvalidDocumentStateError(
            document_id, DocumentStatus.READY.value, document.status
        )
    return (
        db.query(DocumentChunk)
        .filter(DocumentChunk.document_id == document_id)
        .order_by(DocumentChunk.chunk_index.asc())
        .all()
    )


def delete_document(db: Session, document_id: int) -> None:
    """删除文档：先删 DB 行（成功）再清理磁盘文件。

    chunks 与 chunk_embeddings 由外键 CASCADE 删除（SQLite 需显式启用
    FK pragma，已在连接层开启）；关键词索引行无外键，须显式清理
    （同一事务内先删索引行，杜绝幽灵记录；PG 回退无索引表，无操作）。
    """
    document = get_document(db, document_id)
    storage_key = document.storage_key
    get_search_backend().delete_keyword_rows(db, document_id)
    db.delete(document)
    db.commit()
    storage_dir = document_storage.ensure_storage_dir(settings.document_storage_dir)
    document_storage.delete_file(storage_dir, storage_key)
