"""检索 API 路由（Phase 4B）：建立/重建索引、搜索（keyword/vector/hybrid）。

错误约定：检索领域错误统一经 main.py 的 RetrievalError handler 映射为
稳定状态码（404/409/422/500/503），文案 sanitized（不含路径/向量/SQL/
Traceback）。索引失败（INDEXING_FAILED 500）不会吞异常——文档已回滚到
INDEX_FAILED 状态，调用方明确知道发生了什么。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.retrieval import SearchRequest, SearchResponse
from app.services.retrieval_index_service import RetrievalIndexService
from app.services.retrieval_service import RetrievalService

router = APIRouter(prefix="/api/retrieval", tags=["retrieval"])


def _index_response(document_id: int, stats) -> dict:
    return {
        "document_id": document_id,
        "index_status": "INDEXED",
        **stats.to_dict(),
    }


@router.post("/index/{document_id}", summary="建立检索索引（幂等：内容未变则跳过）")
def index_document(document_id: int, db: Session = Depends(get_db)):
    stats = RetrievalIndexService(db).index_document(document_id)
    return _index_response(document_id, stats)


@router.post("/reindex/{document_id}", summary="强制重建检索索引（清空后全量重算）")
def reindex_document(document_id: int, db: Session = Depends(get_db)):
    stats = RetrievalIndexService(db).reindex_document(document_id)
    return _index_response(document_id, stats)


@router.post("/search", response_model=SearchResponse, summary="检索文档（默认 hybrid）")
def search(body: SearchRequest, db: Session = Depends(get_db)):
    return RetrievalService(db).search(
        query=body.query,
        mode=body.mode.value,
        top_k=body.top_k,
        document_ids=body.document_ids,
    )
