"""确定性文本切块（Phase 4A）。

无 LLM、无随机性：给定相同的 (text, chunk_size, overlap) 永远产生相同的
块序列，全部边界规则显式可测：

- 安全切割点优先：句末标点（。！？…！？；;) 或换行之后的位置；
- 窗口 [pos, pos+chunk_size) 内取最大的安全切割点（贪心，块尽可能长）；
- 没有安全切割点时在 chunk_size 处硬切（确定性，可能切断句子）；
- 相邻块共享固定 overlap 个字符；块短于 overlap 时退化为无重叠
  （保证每次迭代位置严格前进，绝不死循环）；
- 块偏移为原始文本字符区间 [char_start, char_end)（含切割点字符本身）；
- token_estimate = ceil(字符数 / 4)：中文为主的保守近似，不精确、也不冒充精确；
- 空文本产生 0 个块。
"""

import math

# 句子/语义边界字符：块尾优先落在这里之后
_SENTENCE_END_CHARS = "。！？!?…；;"

# 换行是段/行边界，优先级与句末标点相同（统一视为安全切割点）
_BOUNDARY_CHARS = _SENTENCE_END_CHARS + "\n"


def _find_safe_cut(text: str, pos: int, limit: int) -> int | None:
    """在 (pos, limit] 内找最大的安全切割位置；找不到返回 None。

    用 str.rfind 在每个边界字符上找最后出现位置（C 实现，确定且快），
    取最大值；切割点包含边界字符本身（pos = index + 1）。
    """
    best: int | None = None
    window = text[pos:limit]
    for char in _BOUNDARY_CHARS:
        idx = window.rfind(char)
        if idx != -1:
            candidate = pos + idx + 1
            if best is None or candidate > best:
                best = candidate
    return best


def chunk_text(
    text: str,
    *,
    chunk_size: int,
    overlap: int,
) -> list[dict]:
    """把纯文本切成确定性块序列。

    每块为 dict：{"content", "char_start", "char_end", "token_estimate"}。
    chunk_size/overlap 必须为正数且 overlap < chunk_size（调用方保证；
    config 层已有同款校验兜底）。
    """
    if chunk_size <= 0 or overlap < 0 or overlap >= chunk_size:
        raise ValueError("chunk_size must be > 0 and overlap must be < chunk_size")
    if not text:
        return []

    chunks: list[dict] = []
    pos = 0
    text_len = len(text)
    while pos < text_len:
        limit = min(pos + chunk_size, text_len)
        cut = _find_safe_cut(text, pos, limit)
        if cut is None:
            # 窗口内没有安全切割点：硬切。限制窗口至少前进 1 字符。
            cut = max(limit, pos + 1)
        content = text[pos:cut]
        chunks.append(
            {
                "content": content,
                "char_start": pos,
                "char_end": cut,
                "token_estimate": math.ceil(len(content) / 4),
            }
        )
        # 下一块起点：cut - overlap；块不足 overlap 长时退化为无重叠
        next_pos = cut - overlap
        if next_pos <= pos:
            next_pos = cut
        pos = next_pos
    return chunks
