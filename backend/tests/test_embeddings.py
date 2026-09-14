"""EmbeddingProvider 单元测试：维度/确定性/跨进程/中英文/normalize/批量/防御。"""

import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from app.embeddings.base import (
    EmbeddingBatchTooLargeError,
    EmbeddingInputError,
    EmbeddingProvider,
)
from app.embeddings.factory import create_embedding_provider
from app.embeddings.local_hash_provider import LocalHashEmbeddingProvider, _tokens

BACKEND_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture()
def provider():
    return LocalHashEmbeddingProvider(dimension=384)


# ---------- 基础契约 ----------


def test_dimension_fixed(provider):
    assert provider.dimension == 384
    vec = provider.embed_query("hello")
    assert len(vec) == 384


def test_same_text_same_vector(provider):
    assert provider.embed_query("你好 JARVIS") == provider.embed_query("你好 JARVIS")


def test_deterministic_across_batch_and_single(provider):
    docs = ["任务提醒", "reminder task", "中英 mixed 混合"]
    batch = provider.embed_documents(docs)
    assert batch == [provider.embed_query(d) for d in docs]


def test_cross_process_identical(provider):
    """跨进程结果一致：持久化向量不依赖进程内状态（不使用内置 hash()）。"""
    text = "中英 mixed 混合文本 JARVIS reminder 2026"
    expected = provider.embed_query(text)
    script = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(BACKEND_DIR)!r})\n"
        "from app.embeddings.local_hash_provider import LocalHashEmbeddingProvider\n"
        f"p = LocalHashEmbeddingProvider(dimension=384)\n"
        f"print(json.dumps(p.embed_query({text!r})))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert expected == json.loads(proc.stdout)


def test_zh_and_en_nonzero(provider):
    """中英文输入都产生非零向量（词汇被哈希进向量）。"""
    for text in ["你好世界", "hello world", "混合 mixed 123"]:
        vec = provider.embed_query(text)
        assert any(value != 0.0 for value in vec)


def test_l2_norm_approx_one(provider):
    for text in ["你好", "hello world", "混合 mixed"]:
        vec = provider.embed_query(text)
        norm = math.sqrt(sum(v * v for v in vec))
        assert norm == pytest.approx(1.0, abs=1e-9)


def test_empty_list_returns_empty_list(provider):
    assert provider.embed_documents([]) == []


def test_batch_order_preserved(provider):
    texts = ["第一段", "second", "第三段内容", "fourth"]
    vectors = provider.embed_documents(texts)
    assert vectors == [provider.embed_query(t) for t in texts]
    assert vectors[0] != vectors[1]  # 顺序区分内容


def test_no_nan_or_infinity(provider):
    for vec in provider.embed_documents(["a" * 5000, "中文" * 1000]):
        for value in vec:
            assert math.isfinite(value)


def test_no_python_builtin_hash_in_provider_source():
    """持久化向量不得依赖内置 hash()（跨进程可能变化）——用 AST 检查真实调用。"""
    import ast

    source = Path(
        BACKEND_DIR / "app" / "embeddings" / "local_hash_provider.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "hash", "builtin hash() must not be used"
        if isinstance(node, ast.Attribute) and node.attr == "hash":
            # 允许 hashlib.sha256 / int.from_bytes；不允许任何 .hash() 调用
            assert False, f"hash attribute call at line {node.lineno}"
    assert "hashlib" in source


# ---------- 输入防御 ----------


def test_blank_query_rejected(provider):
    for blank in ["", "   ", "\n\t"]:
        with pytest.raises(EmbeddingInputError):
            provider.embed_query(blank)


def test_batch_limit(monkeypatch, provider):
    monkeypatch.setattr("app.embeddings.base.settings.embedding_batch_max", 2)
    with pytest.raises(EmbeddingBatchTooLargeError):
        provider.embed_documents(["a", "b", "c"])


def test_text_length_limit(monkeypatch, provider):
    monkeypatch.setattr("app.embeddings.base.settings.embedding_max_text_chars", 100)
    with pytest.raises(EmbeddingInputError):
        provider.embed_query("x" * 101)


def test_blank_inside_batch_rejected(provider):
    with pytest.raises(EmbeddingInputError):
        provider.embed_documents(["ok", "  ", "fine"])


def test_non_finite_values_defended():
    """基类防御：非有限数值被拒绝（不崩溃、不静默）。"""

    class EvilProvider(EmbeddingProvider):
        dimension = 8
        model_name = "evil-test"

        @property
        def dimension(self) -> int:  # type: ignore[override]
            return 8

        @property
        def model_name(self) -> str:
            return "evil-test"

        def _embed_batch(self, texts: list[str]) -> list[list[float]]:
            return [[float("inf") if i == 0 else float("nan")] + [0.0] * 7 for i in range(len(texts))]

    evil = EvilProvider()
    from app.embeddings.base import EmbeddingInternalError

    with pytest.raises(EmbeddingInternalError):
        evil.embed_query("hello")


# ---------- token 化细节 ----------


def test_tokens_english_lowercased():
    assert list(_tokens("Hello World 123")) == ["hello", "world", "123"]


def test_tokens_cjk_ngrams():
    assert list(_tokens("你好")) == ["你", "你好", "好"]
    assert list(_tokens("任务提醒")) == ["任", "任务", "务", "务提", "提", "提醒", "醒"]


def test_tokens_mixed():
    toks = list(_tokens("JARVIS提醒"))
    assert "jarvis" in toks
    assert "提醒" in toks and "JARVIS提醒".count(")") == 0


# ---------- 工厂 ----------


def test_factory_default_is_local_hash():
    provider = create_embedding_provider()
    assert isinstance(provider, LocalHashEmbeddingProvider)
    assert provider.model_name == "local-hash-v1"


def test_factory_unknown_provider_fails():
    from app.embeddings.base import EmbeddingError

    with pytest.raises(EmbeddingError) as exc_info:
        create_embedding_provider(name="sentence-transformers")
    assert exc_info.value.code == "UNKNOWN_EMBEDDING_PROVIDER"


def test_config_rejects_unknown_provider():
    from app.core.config import Settings

    with pytest.raises(ValueError, match="embedding_provider"):
        Settings(embedding_provider="openai-embeddings", _env_file=None)
