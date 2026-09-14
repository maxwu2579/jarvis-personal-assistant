"""引用验证器测试（测试矩阵 C）：allowlist 验证 / 去重 / 顺序 / 权威引用。

关键防御：模型伪造的任何权威字段（标题、chunk 偏移、quote）都不生效——
引用信息全部由后端从数据库 chunk 生成。
"""

import pytest

from app.schemas.rag import CitationOut
from app.services.citation_validator import CitationValidator, build_quote
from app.services.rag_context_builder import EvidenceBlock
from app.services.rag_errors import RAGCitationInvalidError

CHUNK_CONTENT = "团队例会每周五下午3点整准时召开，风雨无阻。"


def _block(label: str, *, content: str = CHUNK_CONTENT, chunk_id: int = 1) -> EvidenceBlock:
    return EvidenceBlock(
        label=label,
        document_id=10,
        document_title="meeting.txt",
        chunk_id=chunk_id,
        chunk_index=0,
        char_start=12,
        char_end=12 + len(content),
        content=content,
        retrieval_score=0.5,
        keyword_score=0.0,
        vector_score=0.4,
        retrieval_mode="hybrid",
    )


def _validator(*labels: str) -> CitationValidator:
    return CitationValidator({label: _block(label) for label in labels})


# ---------- 标签验证 ----------


def test_valid_labels_kept_in_evidence_order():
    v = _validator("C1", "C2", "C3")
    # 模型乱序 + 重复 → 按证据顺序去重
    assert v.validate_labels(["C3", "C1", "C3", "C2"], sufficient=True) == [
        "C1",
        "C2",
        "C3",
    ]


def test_unknown_label_rejected():
    v = _validator("C1", "C2")
    with pytest.raises(RAGCitationInvalidError) as exc:
        v.validate_labels(["C1", "C999"], sufficient=True)
    assert exc.value.invalid_labels == ["C999"]


def test_non_c_pattern_rejected():
    v = _validator("C1")
    with pytest.raises(RAGCitationInvalidError) as exc:
        v.validate_labels(["evil"], sufficient=True)
    assert exc.value.invalid_labels == ["evil"]


def test_duplicate_invalid_labels_capped_at_ten():
    v = _validator("C1")
    with pytest.raises(RAGCitationInvalidError) as exc:
        v.validate_labels(["C99"] * 50, sufficient=True)
    assert len(exc.value.invalid_labels) == 10


def test_sufficient_true_without_labels_rejected():
    """声称证据充分却零引用：无支撑答案不可接受。"""
    v = _validator("C1")
    with pytest.raises(RAGCitationInvalidError) as exc:
        v.validate_labels([], sufficient=True)
    assert exc.value.no_labels is True


def test_insufficient_without_labels_allowed():
    """sufficient=false 时零引用是正常拒答路径。"""
    v = _validator("C1")
    assert v.validate_labels([], sufficient=False) == []


def test_sufficient_false_with_labels_allowed():
    """sufficient=false 但给了标签：不矛盾（模型可以引用后仍判不足）。"""
    v = _validator("C1")
    assert v.validate_labels(["C1"], sufficient=False) == ["C1"]


# ---------- 权威引用构建 ----------


def test_build_citations_backend_authoritative():
    v = _validator("C1", "C2")
    citations = v.build_citations(["C1", "C2"], retrieval_mode="hybrid")
    assert len(citations) == 2
    c1 = citations[0]
    assert isinstance(c1, CitationOut)
    assert c1.citation_id == "C1"
    assert c1.document_id == 10
    assert c1.document_title == "meeting.txt"
    assert c1.chunk_id == 1
    assert c1.chunk_index == 0
    assert c1.char_start == 12
    assert c1.char_end == 12 + len(CHUNK_CONTENT)
    assert c1.retrieval_score == 0.5
    assert c1.retrieval_mode == "hybrid"
    # quote 是 chunk 真实内容的子串
    assert c1.quote == CHUNK_CONTENT
    assert c1.quote in _block("C1").content


def test_quote_truncation_is_real_prefix(monkeypatch):
    monkeypatch.setattr("app.services.citation_validator.settings.rag_quote_max_chars", 8)
    content = "abcdefghijklmnop"
    quote = build_quote(content)
    assert quote == "abcdefgh"
    assert quote in content  # 真实连续子串（前缀）


def test_quote_never_from_model():
    """模型给多长的伪 quote 都无效：build_citations 只用块内容。"""
    v = _validator("C1")
    citations = v.build_citations(["C1"], retrieval_mode="hybrid")
    assert citations[0].quote == CHUNK_CONTENT
