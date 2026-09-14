"""确定性切块测试：边界规则、重叠、偏移、token 估算、非法参数、可复现性。"""

import pytest

from app.services.chunking import chunk_text


def _rebuild(chunks: list[dict], overlap: int) -> str:
    """按块序与重叠量还原原文（验证切块无损）。

    规则：首块全取，其后每块跳过实际重叠前缀。实际重叠 = min(overlap,
    前一块长度)：当块短于 overlap 时切块算法退化为无重叠（见 chunking 模块）。
    """
    if not chunks:
        return ""
    parts = [chunks[0]["content"]]
    for chunk in chunks[1:]:
        prev_len = len(parts[-1])
        parts.append(chunk["content"][min(overlap, prev_len):])
    return "".join(parts)


def test_empty_text_yields_no_chunks():
    assert chunk_text("", chunk_size=1200, overlap=200) == []


def test_short_text_is_single_chunk():
    text = "你好，JARVIS。"  # 3 + 6 + 1 = 10 个字符
    chunks = chunk_text(text, chunk_size=1200, overlap=200)
    assert len(chunks) == 1
    assert chunks[0]["content"] == text
    assert (chunks[0]["char_start"], chunks[0]["char_end"]) == (0, 10)
    assert chunks[0]["token_estimate"] == 3  # ceil(10/4)


def test_split_at_sentence_end_prefers_longest_boundary():
    # 两个句号，块大小允许装下第一句但装不下两句 → 在第一句句号后切
    text = "第一句话。第二句话。第三句话。"
    chunks = chunk_text(text, chunk_size=8, overlap=0)
    assert chunks[0]["content"] == "第一句话。"
    assert chunks[1]["char_start"] == 5


def test_split_at_newline():
    text = "line one\nline two\nline three"
    chunks = chunk_text(text, chunk_size=11, overlap=0)
    assert chunks[0]["content"] == "line one\n"
    assert chunks[1]["char_start"] == 9


def test_hard_cut_when_no_boundary():
    # 无任何边界字符的长串 → 在 chunk_size 处硬切
    text = "a" * 25
    chunks = chunk_text(text, chunk_size=10, overlap=0)
    assert [c["content"] for c in chunks] == ["a" * 10, "a" * 10, "a" * 5]
    assert [c["char_start"] for c in chunks] == [0, 10, 20]
    assert [c["char_end"] for c in chunks] == [10, 20, 25]


def test_overlap_between_adjacent_chunks():
    text = ("第一句。第二句。第三句。第四句。" * 4) * 10
    chunks = chunk_text(text, chunk_size=30, overlap=5)
    assert len(chunks) >= 2
    for prev, cur in zip(chunks, chunks[1:]):
        # 相邻块共享恰好 overlap 个字符（块长都超过 overlap 时）
        assert cur["content"][:5] == prev["content"][-5:]


def test_overlap_degrades_when_chunk_shorter_than_overlap():
    # 块比 overlap 短 → 无重叠但位置必须严格前进（绝不死循环）
    text = "。".join("字" for _ in range(100))  # 每块（不含标点）可能很短
    chunks = chunk_text(text, chunk_size=8, overlap=6)
    starts = [c["char_start"] for c in chunks]
    assert all(b > a for a, b in zip(starts, starts[1:]))


def test_char_offsets_are_contiguous_and_bounded():
    text = "这是测试文本。" * 30
    chunks = chunk_text(text, chunk_size=16, overlap=4)
    assert chunks[0]["char_start"] == 0
    assert chunks[-1]["char_end"] == len(text)
    for prev, cur in zip(chunks, chunks[1:]):
        assert cur["char_start"] <= prev["char_end"]
    for chunk in chunks:
        assert chunk["char_start"] < chunk["char_end"]
        assert chunk["char_end"] <= len(text)


def test_rebuild_recovers_original_text():
    text = "句子边界测试。" * 50 + "\n" + "无边界" * 500
    overlap = 7
    chunks = chunk_text(text, chunk_size=40, overlap=overlap)
    assert _rebuild(chunks, overlap) == text


def test_token_estimate_is_ceil_of_quarter():
    chunks = chunk_text("a" * 9, chunk_size=100, overlap=0)
    assert chunks[0]["token_estimate"] == 3  # ceil(9/4)
    chunks = chunk_text("a" * 8, chunk_size=100, overlap=0)
    assert chunks[0]["token_estimate"] == 2  # ceil(8/4)


def test_deterministic_same_input_same_output():
    text = "确定性检查。" * 100
    a = chunk_text(text, chunk_size=50, overlap=10)
    b = chunk_text(text, chunk_size=50, overlap=10)
    assert a == b


@pytest.mark.parametrize(
    "chunk_size, overlap",
    [
        (0, 0),  # 块大小必须 > 0
        (-5, 1),
        (10, 10),  # 重叠必须严格小于块大小
        (10, 11),
        (10, -1),  # 重叠不能为负
    ],
)
def test_invalid_parameters_raise(chunk_size, overlap):
    with pytest.raises(ValueError):
        chunk_text("abc", chunk_size=chunk_size, overlap=overlap)
