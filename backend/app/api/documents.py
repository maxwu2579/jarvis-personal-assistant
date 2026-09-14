"""文档 API 路由（Phase 4A）：上传 / 列表 / 详情 / 块预览 / 删除。

统一错误结构 {"error": {"code", "message"}}：所有业务错误（含请求本身
非法与超限快速拒绝）都经 main.py 的 DocumentError handler 映射，响应
结构与 Pydantic 校验错误一致（不产生 {"detail": ...} 包装）。
"""

from fastapi import APIRouter, Depends, File, UploadFile
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.schemas.document import DocumentChunkOut, DocumentOut
from app.services import document_service
from app.services.document_errors import DocumentValidationError, FileTooLargeError

router = APIRouter(prefix="/api/documents", tags=["documents"])


@router.post("", response_model=DocumentOut, status_code=201, summary="上传并处理文档")
async def upload_document(
    file: UploadFile = File(...), db: Session = Depends(get_db)
):
    filename = (file.filename or "").strip()
    if not filename:
        raise DocumentValidationError("filename is required")

    # 超限快速拒绝：依据已声明的 size（不读取正文）；未声明时读入后由
    # service 复核（双保险，避免大文件占满内存）。
    max_bytes = settings.document_max_file_size_mb * 1024 * 1024
    declared_size = getattr(file, "size", None)
    if declared_size is not None and declared_size > max_bytes:
        raise FileTooLargeError(
            f"file is too large ({declared_size} bytes > {max_bytes} bytes)"
        )

    content = await file.read()
    return document_service.upload_and_process(
        db,
        filename=filename,
        content_type=file.content_type or "",
        content=content,
    )


def _with_index_count(doc, count: int) -> DocumentOut:
    """校验 ORM 后覆盖已索引块数（旧版 pydantic 无 model_validate(update=)）。"""
    return DocumentOut.model_validate(doc).model_copy(
        update={"indexed_chunk_count": count}
    )


@router.get("", response_model=list[DocumentOut], summary="文档列表")
def list_documents(db: Session = Depends(get_db)):
    docs = document_service.list_documents(db)
    counts = document_service.indexed_chunk_counts(db, [d.id for d in docs])
    return [_with_index_count(doc, counts.get(doc.id, 0)) for doc in docs]


@router.get("/{document_id}", response_model=DocumentOut, summary="获取单个文档")
def get_document(document_id: int, db: Session = Depends(get_db)):
    doc = document_service.get_document(db, document_id)
    counts = document_service.indexed_chunk_counts(db, [doc.id])
    return _with_index_count(doc, counts.get(doc.id, 0))


@router.get("/{document_id}/chunks", response_model=list[DocumentChunkOut], summary="文档块预览")
def list_chunks(document_id: int, db: Session = Depends(get_db)):
    """仅 READY 文档可查块；内容为预览（截断），非完整原文。"""
    return document_service.list_chunks(db, document_id)


@router.delete("/{document_id}", summary="删除文档（连同磁盘文件与块）")
def delete_document(document_id: int, db: Session = Depends(get_db)):
    document_service.delete_document(db, document_id)
    return {"deleted": True}
