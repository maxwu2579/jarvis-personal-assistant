"""共享 tokenizer（Phase 5A）单元测试：FTS5 与 ILIKE 回退共用同一套 token 规则。

保证两方言的检索入口（token 化语义）一致；纯文本处理，无 SQL。
"""

import pytest

from app.services.keyword_tokens import build_cjk_grams, scan_query_tokens


class TestScanQueryTokens:
    def test_english_words_lowercased_and_ordered(self):
        assert scan_query_tokens("Hello WORLD") == ["hello", "world"]

    def test_cjk_bigram_segmentation(self):
        # 双字 CJK 段 → 单个 bigram（比 unigram 精确）
        assert scan_query_tokens("提醒") == ["提醒"]

    def test_cjk_multi_char_sliding_bigrams(self):
        assert scan_query_tokens("周报提醒") == ["周报", "报提", "提醒"]

    def test_single_cjk_char_falls_back_to_unigram(self):
        assert scan_query_tokens("的") == ["的"]

    def test_mixed_en_and_cjk(self):
        assert scan_query_tokens("JARVIS周报 reminder") == [
            "jarvis",
            "周报",
            "reminder",
        ]

    def test_dedup_preserves_first_occurrence_order(self):
        assert scan_query_tokens("提醒 提醒") == ["提醒"]
        assert scan_query_tokens("jarvis 提醒 jarvis") == ["jarvis", "提醒"]

    def test_punctuation_ignored(self):
        assert scan_query_tokens("，。！？ ") == []
        assert scan_query_tokens("") == []

    def test_numbers_are_tokens(self):
        assert scan_query_tokens("版本 4.2 升级") == ["版本", "4", "2", "升级"]


class TestBuildCjkGrams:
    def test_english_grams(self):
        assert build_cjk_grams("JARVIS") == "jarvis"

    def test_cjk_unigram_plus_bigram(self):
        # 索引侧：unigram + bigram（查询侧只发 bigram 是刻意的不对称，见 docstring）
        assert build_cjk_grams("提醒") == "提 提醒 醒"

    def test_mixed_content(self):
        assert build_cjk_grams("JARVIS周报") == "jarvis 周 周报 报"

    def test_empty_input(self):
        assert build_cjk_grams("") == ""
