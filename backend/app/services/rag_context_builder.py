"""RAG 证据上下文构建（Phase 4C）。

输入：Phase 4B 检索结果（SearchItem，内容为预览截断）。
输出：证据块列表 + 序列化上下文文本，全程确定性。

规则：
1. 去重：同一 chunk 只出现一次（按检索稳定排序取首次出现）；
2. 稳定排序：(-final_score, document_id, chunk_index)（与检索服务一致，
   分数相同则按文档/块序，结果可复现）；
3. 内容权威性：块的完整内容从数据库重新加载（检索结果里的 content 是
   300 字符预览，不能作为证据全文）；
4. 预算：总字符数/块 token 估算超出 rag_max_context_chars 时，从尾部
   丢弃整块（绝不切块截断内容——引用元数据与 quote 的完整性不受影响），
   丢弃发生时在日志与 BuiltContext.truncated 中明确记录；
5. 标签分配：C1、C2…… 按排序后的证据顺序分配（后端唯一事实来源）；
6. 边界转义：证据文本（内容与标题）中的 `<` / `>` 转义为 &lt; / &gt;，
   文档内伪造的 <EVIDENCE>/</EVIDENCE> 无法破坏真实边界；system prompt
   明确告知模型证据块内转义字符是文档原文；
7. 不把完整本地文件路径传给模型（标题是 original_filename，仅为展示名）。
"""

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.document import Document, DocumentChunk
from app.services.retrieval_service import SearchItem

logger = logging.getLogger(__name__)


@dataclass
class EvidenceBlock:
    """一条证据：标签 + 权威 chunk 定位信息 + 完整内容。"""

    label: str
    document_id: int
    document_title: str
    chunk_id: int
    chunk_index: int
    char_start: int
    char_end: int
    content: str
    retrieval_score: float
    keyword_score: float
    vector_score: float
    retrieval_mode: str


@dataclass
class BuiltContext:
    """构建结果：证据列表、序列化文本与预算统计。"""

    evidence: list[EvidenceBlock]
    context_text: str
    total_chars: int
    total_token_estimate: int
    truncated: bool
    dropped_by_score: int = 0


def escape_evidence_text(text: str) -> str:
    """边界转义：只转义 < 与 >，防止文档内容伪造/破坏 EVIDENCE 边界。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class RAGContextBuilder:
    def __init__(self, db: Session, *, max_total_chars: int | None = None):
        self.db = db
        # 显式传入便于测试；默认从配置读取
        self.max_total_chars = max_total_chars or settings.rag_max_context_chars

    def build(
        self,
        items: list[SearchItem],
        *,
        retrieval_mode: str,
        min_vector_score: float | None = None,
    ) -> BuiltContext:
        """构建证据上下文。

        min_vector_score：向量侧最小命中分数闸门（RAG 专用，检索 UI 不受影响）。
        RRF 融合分是秩次函数（与相似度绝对值无关），不能直接阈值化，因此在
        块级按「keyword 命中（FTS 真实匹配）或 vector 余弦 ≥ 阈值」过滤：
        - keyword 模式：不传闸门，全部保留；
        - vector/hybrid：纯向量命中的弱分块（语义近似的假命中）被丢弃；
        丢弃的块计入 dropped_by_score，不进入上下文与引用。
        """
        if not items:
            return BuiltContext([], "", 0, 0, truncated=False)

        # 1. 去重（同一 chunk 只保留检索序首次出现）
        seen: set[int] = set()
        unique: list[SearchItem] = []
        for item in items:
            if item.chunk_id in seen:
                continue
            seen.add(item.chunk_id)
            unique.append(item)

        # 2. 稳定排序（与检索服务同一规则）
        unique.sort(
            key=lambda item: (
                -item.final_score,
                item.document_id,
                item.chunk_index,
            )
        )

        # 3. 从数据库加载完整内容（不信任检索结果中的预览）
        rows = (
            self.db.query(DocumentChunk, Document)
            .join(Document, Document.id == DocumentChunk.document_id)
            .filter(DocumentChunk.id.in_([item.chunk_id for item in unique]))
            .all()
        )
        full_by_chunk: dict[int, tuple[DocumentChunk, Document]] = {
            chunk.id: (chunk, document) for chunk, document in rows
        }

        # 3.5 分数闸门：keyword 命中保留，纯向量命中需 ≥ 阈值（弱分假命中丢弃）
        dropped_by_score = 0
        if min_vector_score is not None:
            gated: list[SearchItem] = []
            for item in unique:
                if item.keyword_score > 0 or item.vector_score >= min_vector_score:
                    gated.append(item)
                else:
                    dropped_by_score += 1
            if dropped_by_score:
                logger.info(
                    "rag.evidence_gate dropped=%s threshold=%s kept=%s",
                    dropped_by_score,
                    min_vector_score,
                    len(gated),
                )
            unique = gated
            if not unique:
                return BuiltContext(
                    [], "", 0, 0, truncated=False, dropped_by_score=dropped_by_score
                )

        # 4. 预算：贪心前缀，超限时从尾部丢弃整块（truncated 明确记录）
        evidence: list[EvidenceBlock] = []
        total_chars = 0
        total_tokens = 0
        truncated = False
        for item in unique:
            pair = full_by_chunk.get(item.chunk_id)
            if pair is None:
                continue  # 索引与 chunks 不一致的防御（不崩溃）
            chunk, document = pair
            chunk_chars = len(chunk.content)
            if total_chars + chunk_chars > self.max_total_chars:
                truncated = True
                break
            total_chars += chunk_chars
            total_tokens += chunk.token_estimate
            evidence.append(
                EvidenceBlock(
                    label=f"C{len(evidence) + 1}",
                    document_id=document.id,
                    document_title=document.original_filename,
                    chunk_id=chunk.id,
                    chunk_index=chunk.chunk_index,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    content=chunk.content,
                    retrieval_score=item.final_score,
                    keyword_score=item.keyword_score,
                    vector_score=item.vector_score,
                    retrieval_mode=retrieval_mode,
                )
            )

        if truncated:
            logger.info(
                "rag.context_truncated kept=%s dropped=%s total_chars=%s budget=%s",
                len(evidence),
                len(unique) - len(evidence),
                total_chars,
                self.max_total_chars,
            )

        # 5. 序列化（边界转义）
        context_text = "\n\n".join(
            f'<EVIDENCE id="{block.label}" document="{escape_evidence_text(block.document_title)}" '
            f'chunk_index="{block.chunk_index}">\n'
            f"{escape_evidence_text(block.content)}\n"
            f"</EVIDENCE>"
            for block in evidence
        )

        return BuiltContext(
            evidence=evidence,
            context_text=context_text,
            total_chars=total_chars,
            total_token_estimate=total_tokens,
            truncated=truncated,
            dropped_by_score=dropped_by_score,
        )
