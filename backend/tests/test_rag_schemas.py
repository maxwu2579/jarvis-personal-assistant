"""RAG schema 测试（测试矩阵 A）：请求严格校验 + 模型输出最小契约。

- 请求：空白/超长 question、额外字段、非法 mode/top_k/document_ids/language
  → 422 VALIDATION_ERROR（统一错误结构）；
- 模型输出：缺字段/类型错 → 硬失败；多余字段忽略；answer 去空白；
  标签去空白。
"""

import pytest
from pydantic import ValidationError

from app.schemas.rag import (
    CITATION_LABEL_RE,
    RAGAnswerResponse,
    RAGAnswerStatus,
    RAGAskRequest,
    RAGModelOutput,
    RAGRetrievalMeta,
    RAGUsageOut,
)


# ---------- 请求 schema：合法 ----------


def test_ask_request_defaults():
    req = RAGAskRequest(question="国庆假期安排是什么？")
    assert req.question == "国庆假期安排是什么？"
    assert req.document_ids is None
    assert req.retrieval_mode.value == "hybrid"
    assert req.top_k == 5
    assert req.conversation_id is None
    assert req.language.value == "auto"


def test_ask_request_accepts_full_valid_payload():
    req = RAGAskRequest(
        question="例会时间？",
        document_ids=[1, 2],
        retrieval_mode="vector",
        top_k=10,
        conversation_id=7,
        language="zh",
    )
    assert req.retrieval_mode.value == "vector"
    assert req.language.value == "zh"


# ---------- 请求 schema：非法输入（直接 Pydantic 层） ----------


def test_blank_question_rejected():
    with pytest.raises(ValidationError):
        RAGAskRequest(question="   ")


def test_oversized_question_rejected():
    with pytest.raises(ValidationError):
        RAGAskRequest(question="x" * 2001)


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        RAGAskRequest(question="问题", bogus_field="nope")


def test_duplicate_document_ids_rejected():
    with pytest.raises(ValidationError):
        RAGAskRequest(question="问题", document_ids=[3, 3])


def test_too_many_document_ids_rejected():
    """上限 50：超大列表拒绝（防巨型 IN 子句打爆 SQLite 变量数）。"""
    with pytest.raises(ValidationError):
        RAGAskRequest(question="问题", document_ids=list(range(51)))
    RAGAskRequest(question="问题", document_ids=list(range(50)))  # 恰好 50 合法


def test_top_k_out_of_range_rejected():
    with pytest.raises(ValidationError):
        RAGAskRequest(question="问题", top_k=0)
    with pytest.raises(ValidationError):
        RAGAskRequest(question="问题", top_k=11)


def test_invalid_mode_and_language_rejected():
    with pytest.raises(ValidationError):
        RAGAskRequest(question="问题", retrieval_mode="bm25")
    with pytest.raises(ValidationError):
        RAGAskRequest(question="问题", language="fr")


# ---------- 模型输出契约：RAGModelOutput ----------


def test_model_output_minimal_ok():
    out = RAGModelOutput(
        answer="答案", cited_labels=["C1"], sufficient_evidence=True
    )
    assert out.answer == "答案"
    assert out.cited_labels == ["C1"]


def test_model_output_missing_answer_rejected():
    with pytest.raises(ValidationError):
        RAGModelOutput(sufficient_evidence=True)


def test_model_output_blank_answer_rejected():
    with pytest.raises(ValidationError):
        RAGModelOutput(answer="   ", sufficient_evidence=True)


def test_model_output_answer_stripped():
    out = RAGModelOutput(answer="  答案  ", cited_labels=["C1"])
    assert out.answer == "答案"


def test_model_output_oversized_answer_rejected():
    with pytest.raises(ValidationError):
        RAGModelOutput(answer="x" * 4001, sufficient_evidence=True)


def test_model_output_extra_fields_ignored():
    """真实模型的多余字段没有权威性：宽松解析（extra=ignore），忽略不报错。"""
    out = RAGModelOutput(
        answer="答案",
        cited_labels=["C1"],
        sufficient_evidence=True,
        document_id=999,  # 模型伪造的权威字段：解析层不报错，验证层不使用
        title="fake title",
        quote="fake quote",
        offset=12345,
    )
    # 模型伪造的权威字段在解析层被忽略（无该属性），不会进入响应
    assert not hasattr(out, "document_id")
    assert not hasattr(out, "title")
    assert out.cited_labels == ["C1"]


def test_model_output_labels_stripped_and_typed():
    out = RAGModelOutput(answer="a", cited_labels=[" C1 ", "C2"])
    assert out.cited_labels == ["C1", "C2"]
    with pytest.raises(ValidationError):
        RAGModelOutput(answer="a", cited_labels=[123])


def test_citation_label_regex():
    assert CITATION_LABEL_RE == r"^C\d+$"
    import re

    assert re.fullmatch(CITATION_LABEL_RE, "C1")
    assert re.fullmatch(CITATION_LABEL_RE, "C123")
    assert not re.fullmatch(CITATION_LABEL_RE, "C")
    assert not re.fullmatch(CITATION_LABEL_RE, "1C")
    assert not re.fullmatch(CITATION_LABEL_RE, "c1")
    assert not re.fullmatch(CITATION_LABEL_RE, "C999  ")


# ---------- 响应 schema ----------


def test_answer_response_shape():
    resp = RAGAnswerResponse(
        answer="答",
        status=RAGAnswerStatus.ANSWERED,
        citations=[],
        retrieval=RAGRetrievalMeta(mode="hybrid", candidate_count=3, returned_count=1),
        usage=RAGUsageOut(
            model="fake-model", prompt_tokens=10, completion_tokens=5, latency_ms=42
        ),
        conversation_id=None,
        user_message_id=None,
        assistant_message_id=None,
    )
    data = resp.model_dump()
    assert data["status"] == "ANSWERED"
    assert data["usage"]["model"] == "fake-model"


def test_insufficient_status_usage_none():
    """零证据拒答：未调用模型，usage.model 为 None。"""
    resp = RAGAnswerResponse(
        answer="证据不足",
        status=RAGAnswerStatus.INSUFFICIENT_EVIDENCE,
        citations=[],
        retrieval=RAGRetrievalMeta(mode="hybrid", candidate_count=0, returned_count=0),
        usage=RAGUsageOut(model=None, prompt_tokens=None, completion_tokens=None, latency_ms=None),
        conversation_id=None,
        user_message_id=None,
        assistant_message_id=None,
    )
    assert resp.status is RAGAnswerStatus.INSUFFICIENT_EVIDENCE
    assert resp.usage.model is None
