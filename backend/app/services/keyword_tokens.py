"""关键词 token 化（跨方言共享，Phase 5A）。

SQLite 分支的 FTS5 索引/查询与 PostgreSQL 分支的 ILIKE 回退共用同一套
token 规则，保证两方言的检索入口（候选集、稳定次序）语义一致。
本模块是纯文本处理，不含任何 SQL。
"""

import re

# 英文/数字 token（ASCII 小写归一）
_EN_WORD_RE = re.compile(r"[a-z0-9]+")
# CJK 统一表意文字（含扩展 A）
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]+")


def _cjk_ngrams(segment: str) -> list[str]:
    """CJK 段内生成 unigram + bigram（滑动窗口 1-2）。"""
    grams: list[str] = []
    for i, char in enumerate(segment):
        grams.append(char)
        if i + 1 < len(segment):
            grams.append(segment[i : i + 2])
    return grams


def _scan_tokens(text: str) -> list[str]:
    """英文词（小写）+ CJK n-gram(1-2)，保序。"""
    tokens: list[str] = []
    lower = text.lower()
    pos = 0
    length = len(lower)
    while pos < length:
        m = _EN_WORD_RE.match(lower, pos)
        if m:
            tokens.append(m.group(0))
            pos = m.end()
            continue
        m = _CJK_RE.match(lower, pos)
        if m:
            tokens.extend(_cjk_ngrams(m.group(0)))
            pos = m.end()
            continue
        pos += 1
    return tokens


def _dedup_ordered(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token not in seen:
            seen.add(token)
            out.append(token)
    return out


def build_cjk_grams(text: str) -> str:
    """索引侧：文本的检索 gram 序列（英文词 + 中文 unigram/bigram）。"""
    return " ".join(_scan_tokens(text))


def scan_query_tokens(query: str) -> list[str]:
    """查询侧 token：英文词 + CJK bigram（CJK 段只有单字时用 unigram），去重保序。

    与索引侧（unigram+bigram）不对称是刻意的：bigram 比 unigram 精确，
    召回缺口由混合模式（vector/hybrid）的向量路补充（与 FTS5 时期一致）。
    """
    tokens: list[str] = []
    lower = query.lower()
    pos = 0
    length = len(lower)
    while pos < length:
        m = _EN_WORD_RE.match(lower, pos)
        if m:
            tokens.append(m.group(0))
            pos = m.end()
            continue
        m = _CJK_RE.match(lower, pos)
        if m:
            segment = m.group(0)
            if len(segment) == 1:
                tokens.append(segment)
            else:
                tokens.extend(segment[i : i + 2] for i in range(len(segment) - 1))
            pos = m.end()
            continue
        pos += 1
    return _dedup_ordered(tokens)
