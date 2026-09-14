"""引用验证器（Phase 4C）：模型引用标签 → 权威 CitationOut。

职责：
1. allowlist 验证：模型只能引用本次下发的证据标签（C1、C2……）；
   - 非法标签（含文档注入的 C999 等伪造标签）→ RAGCitationInvalidError；
   - 声称证据充分却零引用 → RAGCitationInvalidError（无支撑的答案不可接受）；
   - 标签去重（同一标签只产生一条引用）；
2. quote 生成：从数据库 chunk 内容确定性截取真实连续子串（前缀截断至
   rag_quote_max_chars），**绝不信任模型给出的标题、offset、quote**；
3. 引用顺序确定：按证据分配顺序（C1、C2……）而非模型输出顺序。
"""

import re

from app.core.config import settings
from app.schemas.rag import CITATION_LABEL_RE, CitationOut
from app.services.rag_context_builder import EvidenceBlock
from app.services.rag_errors import RAGCitationInvalidError


class CitationValidator:
    def __init__(self, evidence_by_label: dict[str, EvidenceBlock]):
        """evidence_by_label：本次下发证据的 allowlist（label -> 证据块）。"""
        self.evidence_by_label = evidence_by_label

    def validate_labels(self, labels: list[str], *, sufficient: bool) -> list[str]:
        """校验模型引用标签；返回按证据顺序去重后的合法标签列表。"""
        if sufficient and not labels:
            raise RAGCitationInvalidError(no_labels=True)

        invalid = [
            label
            for label in labels
            if not re.fullmatch(CITATION_LABEL_RE, label)
            or label not in self.evidence_by_label
        ]
        if invalid:
            raise RAGCitationInvalidError(invalid_labels=invalid[:10])

        # 去重 + 按证据分配顺序排序（确定性输出）
        return [label for label in self.evidence_by_label if label in set(labels)]

    def build_citations(
        self, labels: list[str], *, retrieval_mode: str
    ) -> list[CitationOut]:
        """由合法标签生成权威引用（quote 来自数据库 chunk 的真实子串）。"""
        citations: list[CitationOut] = []
        for label in labels:
            block = self.evidence_by_label[label]
            citations.append(
                CitationOut(
                    citation_id=block.label,
                    document_id=block.document_id,
                    document_title=block.document_title,
                    chunk_id=block.chunk_id,
                    chunk_index=block.chunk_index,
                    char_start=block.char_start,
                    char_end=block.char_end,
                    quote=build_quote(block.content),
                    retrieval_score=block.retrieval_score,
                    retrieval_mode=retrieval_mode,
                )
            )
        return citations


def build_quote(content: str) -> str:
    """从 chunk 完整内容确定性截取真实连续子串（前缀截断，绝不来自模型）。"""
    limit = settings.rag_quote_max_chars
    if len(content) <= limit:
        return content
    return content[:limit]
