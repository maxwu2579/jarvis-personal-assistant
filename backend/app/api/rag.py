"""RAG API 路由（Phase 4C）：POST /api/rag/ask。

请求经 RAGAskRequest 严格验证（extra=forbid → 422 VALIDATION_ERROR）；
业务错误映射：
- 文档不存在 / 未 READY / 未 INDEXED → 404 DOCUMENT_NOT_FOUND / 409（沿用
  既有 DocumentError、RetrievalError handler，语义稳定）；
- 对话不存在 → 404 CONVERSATION_NOT_FOUND；
- LLM 未配置 → 503 LLM_NOT_CONFIGURED；超时/上游 → 502；
- 模型输出/引用验证失败 → 502 RAG_RESPONSE_INVALID / RAG_CITATION_INVALID
  （RAGError handler，文案脱敏）；
- 证据不足 → HTTP 200 + status=INSUFFICIENT_EVIDENCE（正常业务结果）。
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.llm.base import LLMProvider
from app.llm.factory import get_llm_provider
from app.schemas.rag import RAGAnswerResponse, RAGAskRequest
from app.services.rag_service import RagService

router = APIRouter(prefix="/api/rag", tags=["rag"])


@router.post(
    "/ask",
    response_model=RAGAnswerResponse,
    summary="基于已索引文档回答问题（答案与引用均由后端验证）",
)
def ask(body: RAGAskRequest, db: Session = Depends(get_db), provider: LLMProvider = Depends(get_llm_provider)):
    return RagService(db, llm_provider=provider).ask(
        question=body.question,
        document_ids=body.document_ids,
        retrieval_mode=body.retrieval_mode.value,
        top_k=body.top_k,
        conversation_id=body.conversation_id,
        language=body.language.value,
    )
