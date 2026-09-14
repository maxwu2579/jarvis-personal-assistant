"""Grounded RAG 编排（Phase 4C）：检索 → 受约束证据上下文 → LLM → 引用验证。

执行顺序（与 Phase 4C 规范一致，逐条落实）：
1. 请求已在 API 层经 RAGAskRequest 严格验证（extra=forbid）；
2. 验证 document_ids 存在且 READY + INDEXED（404/409，绝不自动建索引）；
3. 复用 Phase 4B RetrievalService（不复制另一套检索逻辑）；
4. 上下文构建：稳定排序、去重、预算裁剪（RAGContextBuilder）；
5. 后端分配不可伪造的引用标签 C1、C2……；
6. 构造 system prompt 与 evidence blocks（数据/指令边界第一层）；
7. 调用 LLMProvider（结构化输出目标 RAGModelOutput）；
8. Pydantic 二次验证模型输出（容忍 Markdown 围栏、容忍多余字段）；
9. 后端验证每个引用标签只引用本次下发的证据（allowlist）；
10. 后端用真实数据库内容生成 CitationOut（不信任模型给出的任何权威字段）；
11. 证据不足（零证据 / 模型 sufficient_evidence=false）→ HTTP 200 +
    INSUFFICIENT_EVIDENCE + 固定拒答文案，不算服务异常；
12. conversation_id 提供时按既有聊天事务规则保存 USER/ASSISTANT；
    模型调用或验证失败 → USER 保留、不伪造 ASSISTANT、审计 FAILED；
13. 返回结构化响应（答案、引用、使用量、检索元数据）。

失败与审计语义（与 chat/proposal 既有约定一致）：
- 成功：ASSISTANT + audit 单事务原子提交（失败整体回滚，不留半成品）；
- 失败：USER 已提交保留；审计行 status=FAILED + error_code 单独提交；
  任何错误响应都稳定、脱敏（不含原始模型输出、路径、Traceback、密钥）。
"""

import json
import logging
import re
import time
import uuid

from sqlalchemy.orm import Session

from app.core.config import settings
from app.embeddings.base import EmbeddingProvider
from app.llm.base import (
    LLMError,
    LLMNotConfiguredError,
    LLMProvider,
    LLMTimeoutError,
    LLMUpstreamError,
)
from app.llm.rag_prompts import build_rag_system_prompt, build_rag_user_prompt
from app.models.conversation import Conversation, Message, MessageRole
from app.models.document import Document, DocumentIndexStatus, DocumentStatus
from app.models.rag_audit import RagAnswer, RagAnswerStatus
from app.models.task import utcnow
from app.schemas.rag import (
    RAGAnswerResponse,
    SearchMode,
    RAGAnswerStatus,
    RAGModelOutput,
    RAGRetrievalMeta,
    RAGUsageOut,
    CitationOut,
)
from app.services.citation_validator import CitationValidator
from app.services.document_errors import DocumentNotFoundError
from app.services.rag_context_builder import BuiltContext, RAGContextBuilder
from app.services.rag_errors import (
    RAGCitationInvalidError,
    RAGError,
    RAGResponseInvalidError,
)
from app.services.retrieval_errors import (
    DocumentNotIndexedError,
    DocumentNotReadyError,
)
from app.services.retrieval_service import RetrievalService

logger = logging.getLogger(__name__)

# 证据不足时的固定拒答文案（清晰友好；证据不足不是错误，是正常业务结果）
ABSTENTION_ANSWER = (
    "无法基于当前检索到的文档内容回答该问题（证据不足）。"
    "I do not have enough evidence from the retrieved documents to answer this question."
)

# 模型原始输出中的 Markdown 围栏剥离（```````json ... ``` 或 ``` ... ```）
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_model_output(raw_text: str) -> RAGModelOutput:
    """解析并二次验证模型输出。

    - 容忍 Markdown 围栏包裹（取第一个代码块）；无围栏时按完整文本解析；
    - 非 JSON / 非对象 / 缺字段 / 类型错误 → RAGResponseInvalidError
      （error_detail 只含字段定位与规则文本，不含原始输出）；
    - 多余字段忽略（extra="ignore"，无权威性）。
    """
    text = (raw_text or "").strip()
    if not text:
        raise RAGResponseInvalidError("model returned empty output")
    fence = _FENCE_RE.search(text)
    candidate = fence.group(1).strip() if fence else text
    try:
        payload = json.loads(candidate)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RAGResponseInvalidError("model output is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise RAGResponseInvalidError("model output is not a JSON object")
    try:
        return RAGModelOutput.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 - 校验失败统一收敛（摘要脱敏）
        errors = getattr(exc, "errors", lambda: [])()
        parts = []
        for error in errors[:3]:
            loc = ".".join(str(part) for part in error.get("loc", []))
            msg = error.get("msg", "")
            parts.append(f"{loc}: {msg}" if loc else msg)
        detail = "; ".join(parts) if parts else "validation failed"
        raise RAGResponseInvalidError(detail) from exc


def _error_code_for(exc: Exception) -> str:
    """失败审计用稳定错误码（不泄露异常原文）。"""
    if isinstance(exc, LLMNotConfiguredError):
        return "LLM_NOT_CONFIGURED"
    if isinstance(exc, LLMTimeoutError):
        return "LLM_TIMEOUT"
    if isinstance(exc, LLMUpstreamError):
        return "LLM_UPSTREAM_ERROR"
    if isinstance(exc, RAGResponseInvalidError):
        return "RAG_RESPONSE_INVALID"
    if isinstance(exc, RAGCitationInvalidError):
        return "RAG_CITATION_INVALID"
    return "INTERNAL_ERROR"


class RagService:
    def __init__(
        self,
        db: Session,
        *,
        llm_provider: LLMProvider,
        embedding_provider: EmbeddingProvider | None = None,
    ):
        self.db = db
        self.llm_provider = llm_provider
        self.embedding_provider = embedding_provider

    # ---- 对外入口 ----

    def ask(
        self,
        *,
        question: str,
        document_ids: list[int] | None,
        retrieval_mode: str,
        top_k: int,
        conversation_id: int | None,
        language: str,
    ) -> RAGAnswerResponse:
        correlation_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        logger.info("rag.ask_started correlation=%s mode=%s top_k=%s", correlation_id, retrieval_mode, top_k)

        # 2. 文档校验：存在 + READY + INDEXED（404/409，不自动建索引）
        self._validate_documents(document_ids)

        # 3. 复用 Phase 4B 检索服务
        retrieval = RetrievalService(
            self.db, provider=self.embedding_provider
        ).search(
            query=question,
            mode=retrieval_mode,
            top_k=top_k,
            document_ids=document_ids,
        )

        # 4. 上下文构建（稳定排序 / 去重 / 预算裁剪 / 分数闸门 / 标签分配）
        #    闸门：RRF 融合分是秩次函数不能直接阈值化，vector/hybrid 模式对
        #    纯向量命中的弱分块按 rag_min_vector_score 过滤（keyword 命中保留）。
        min_vector_score = (
            settings.rag_min_vector_score
            if retrieval_mode in (SearchMode.vector.value, SearchMode.hybrid.value)
            else None
        )
        context = RAGContextBuilder(self.db).build(
            retrieval.items, retrieval_mode=retrieval_mode, min_vector_score=min_vector_score
        )

        # 12. 按既有规则保存 USER 消息（失败也保留；无 conversation 时为 None）
        user_message = self._save_user_message(conversation_id, question)

        # 11. 零证据 → 直接拒答（不调用 LLM）
        if not context.evidence:
            return self._finish(
                correlation_id=correlation_id,
                started=started,
                question=question,
                language=language,
                retrieval_mode=retrieval_mode,
                retrieval_candidates=retrieval.total_candidates,
                context=context,
                conversation_id=conversation_id,
                user_message_id=user_message.id if user_message else None,
                status=RAGAnswerStatus.INSUFFICIENT_EVIDENCE,
                answer=ABSTENTION_ANSWER,
                citations=[],
                model=None,
                prompt_tokens=None,
                completion_tokens=None,
                audit_document_ids=document_ids,
            )

        # 6-7. 构造 prompt 并调用模型（不自动重试）
        system_prompt = build_rag_system_prompt(language)
        user_prompt = build_rag_user_prompt(question, context.context_text)
        provider_messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        evidence_by_label = {block.label: block for block in context.evidence}

        try:
            response = self.llm_provider.generate(
                provider_messages, response_schema=RAGModelOutput
            )
        except LLMError as exc:
            # 12. 失败一致性：USER 保留，不伪造 ASSISTANT，审计 FAILED
            self._record_failed_audit(
                question=question,
                language=language,
                retrieval_mode=retrieval_mode,
                retrieval_candidates=retrieval.total_candidates,
                document_ids=document_ids,
                context=context,
                conversation_id=conversation_id,
                user_message_id=user_message.id if user_message else None,
                exc=exc,
            )
            raise

        # 8. Pydantic 二次验证（失败 → 审计 FAILED + 502，不泄露原始输出）
        try:
            model_output = parse_model_output(response.text)
        except RAGResponseInvalidError as exc:
            self._record_failed_audit(
                question=question,
                language=language,
                retrieval_mode=retrieval_mode,
                retrieval_candidates=retrieval.total_candidates,
                document_ids=document_ids,
                context=context,
                conversation_id=conversation_id,
                user_message_id=user_message.id if user_message else None,
                exc=exc,
            )
            raise

        # 9-10. 引用 allowlist 验证 + 权威 CitationOut（失败 → 审计 FAILED + 502）
        validator = CitationValidator(evidence_by_label)
        try:
            valid_labels = validator.validate_labels(
                model_output.cited_labels,
                sufficient=model_output.sufficient_evidence,
            )
        except RAGCitationInvalidError as exc:
            self._record_failed_audit(
                question=question,
                language=language,
                retrieval_mode=retrieval_mode,
                retrieval_candidates=retrieval.total_candidates,
                document_ids=document_ids,
                context=context,
                conversation_id=conversation_id,
                user_message_id=user_message.id if user_message else None,
                exc=exc,
            )
            raise

        # 11. 模型判定证据不足 → 固定拒答文案（200，不硬编答案）
        if not model_output.sufficient_evidence:
            return self._finish(
                correlation_id=correlation_id,
                started=started,
                question=question,
                language=language,
                retrieval_mode=retrieval_mode,
                retrieval_candidates=retrieval.total_candidates,
                context=context,
                conversation_id=conversation_id,
                user_message_id=user_message.id if user_message else None,
                status=RAGAnswerStatus.INSUFFICIENT_EVIDENCE,
                answer=ABSTENTION_ANSWER,
                citations=[],
                model=response.model,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                audit_document_ids=document_ids,
            )

        citations = validator.build_citations(valid_labels, retrieval_mode=retrieval_mode)
        return self._finish(
            correlation_id=correlation_id,
            started=started,
            question=question,
            language=language,
            retrieval_mode=retrieval_mode,
            retrieval_candidates=retrieval.total_candidates,
            context=context,
            conversation_id=conversation_id,
            user_message_id=user_message.id if user_message else None,
            status=RAGAnswerStatus.ANSWERED,
            answer=model_output.answer,
            citations=citations,
            model=response.model,
            prompt_tokens=response.prompt_tokens,
            completion_tokens=response.completion_tokens,
            audit_document_ids=document_ids,
            audit_labels=valid_labels,
        )

    # ---- 内部实现 ----

    def _validate_documents(self, document_ids: list[int] | None) -> None:
        if not document_ids:
            return
        for document_id in document_ids:
            document = self.db.get(Document, document_id)
            if document is None:
                raise DocumentNotFoundError(document_id)
            if DocumentStatus(document.status) is not DocumentStatus.READY:
                raise DocumentNotReadyError(document_id, document.status)
            if DocumentIndexStatus(document.index_status) is not DocumentIndexStatus.INDEXED:
                raise DocumentNotIndexedError(document_id)

    def _save_user_message(
        self, conversation_id: int | None, question: str
    ) -> Message | None:
        if conversation_id is None:
            return None
        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            from app.services.chat_service import ConversationNotFoundError

            raise ConversationNotFoundError(conversation_id)
        message = Message(
            conversation_id=conversation_id,
            role=MessageRole.USER.value,
            content=question,
        )
        self.db.add(message)
        self.db.commit()
        self.db.refresh(message)
        return message

    def _finish(
        self,
        *,
        correlation_id: str,
        started: float,
        question: str,
        language: str,
        retrieval_mode: str,
        retrieval_candidates: int,
        context: BuiltContext,
        conversation_id: int | None,
        user_message_id: int | None,
        status: RAGAnswerStatus,
        answer: str,
        citations: list[CitationOut],
        model: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        audit_document_ids: list[int] | None,
        audit_labels: list[str] | None = None,
    ) -> RAGAnswerResponse:
        latency_ms = int((time.monotonic() - started) * 1000)

        # ASSISTANT（仅 ANSWERED 与 INSUFFICIENT_EVIDENCE 两种成功结果落库，
        # 内容要么是模型答案要么是固定拒答文案，绝不伪造成功）——与 audit 单事务
        assistant_message = None
        if conversation_id is not None:
            assistant_message = Message(
                conversation_id=conversation_id,
                role=MessageRole.ASSISTANT.value,
                content=answer,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            )
            self.db.add(assistant_message)
            self.db.flush()
            conversation = self.db.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.updated_at = utcnow()

        audit = RagAnswer(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message.id if assistant_message else None,
            question=question,
            language=language,
            retrieval_mode=retrieval_mode,
            document_ids_json=(
                json.dumps(audit_document_ids) if audit_document_ids else None
            ),
            chunk_ids_json=(
                json.dumps([block.chunk_id for block in context.evidence])
                if context.evidence
                else None
            ),
            citation_labels_json=(
                json.dumps(audit_labels) if audit_labels else None
            ),
            status=status.value,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            candidate_count=retrieval_candidates,
            returned_count=len(context.evidence),
            context_chars=context.total_chars,
            context_token_estimate=context.total_token_estimate,
        )
        self.db.add(audit)
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if assistant_message is not None:
            self.db.refresh(assistant_message)
        self.db.refresh(audit)

        logger.info(
            "rag.completed correlation=%s status=%s mode=%s candidates=%s returned=%s "
            "documents=%s context_chars=%s context_tokens=%s model=%s latency_ms=%s "
            "citations=%s",
            correlation_id,
            status.value,
            retrieval_mode,
            retrieval_candidates,
            len(context.evidence),
            audit_document_ids,
            context.total_chars,
            context.total_token_estimate,
            model,
            latency_ms,
            len(citations),
        )
        return RAGAnswerResponse(
            answer=answer,
            status=status,
            citations=citations,
            retrieval=RAGRetrievalMeta(
                mode=retrieval_mode,
                candidate_count=retrieval_candidates,
                returned_count=len(context.evidence),
            ),
            usage=RAGUsageOut(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
            ),
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=assistant_message.id if assistant_message else None,
        )

    def _record_failed_audit(
        self,
        *,
        question: str,
        language: str,
        retrieval_mode: str,
        retrieval_candidates: int,
        document_ids: list[int] | None,
        context: BuiltContext,
        conversation_id: int | None,
        user_message_id: int | None,
        exc: Exception | None = None,
    ) -> None:
        """失败审计：单独事务提交（USER 已保留；不伪造 ASSISTANT）。"""
        error_code = _error_code_for(exc) if exc is not None else "INTERNAL_ERROR"
        self.db.rollback()  # 清理可能失败的旧事务，确保审计行可写
        audit = RagAnswer(
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            assistant_message_id=None,
            question=question,
            language=language,
            retrieval_mode=retrieval_mode,
            document_ids_json=json.dumps(document_ids) if document_ids else None,
            chunk_ids_json=(
                json.dumps([block.chunk_id for block in context.evidence])
                if context.evidence
                else None
            ),
            citation_labels_json=None,
            status=RagAnswerStatus.FAILED.value,
            error_code=error_code,
            candidate_count=retrieval_candidates,
            returned_count=len(context.evidence),
            context_chars=context.total_chars,
            context_token_estimate=context.total_token_estimate,
        )
        self.db.add(audit)
        try:
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        self.db.refresh(audit)
        logger.warning(
            "rag.failed conversation_id=%s user_message_id=%s error_code=%s",
            conversation_id,
            user_message_id,
            error_code,
        )
