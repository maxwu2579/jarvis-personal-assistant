"""LocalHashEmbeddingProvider：零网络、确定性的词汇向量（Phase 4B 检索基线）。

算法（全部标准库，无模型下载、无随机数）：
1. Token 化（中文 + 英文）：
   - 英文/数字：连续 [a-z0-9]+（小写归一）作为一个 token；
   - 中文：连续 CJK 段内生成 unigram + bigram（滑动窗口 n-gram 1-2），
     单字与相邻双字都能命中，避免 FTS 分词器对中文「整段一词」的缺陷；
2. 每个 token 用 hashlib.sha256 的确定性字节派生 (维度下标, 符号)：
   - 下标 = 前 4 字节整数 mod dimension（均匀折叠）；
   - 符号 = 第 5 字节最低位（±1，sign hashing，降低不同词撞同一维时互相抵消的偏差）；
   - 权重 = 词频计数（重复词权重更高，接近词频向量）；
3. 累加后 L2 normalize（零向量保持零向量，不除零）；
4. 不使用 Python 内置 hash()（跨进程/版本可能变化，无法持久化）。

诚实边界（README 与 UI 同步声明）：
- 这是「轻量词汇向量检索 / lightweight lexical-vector retrieval」基线，
  主要捕获词汇重合（含字面 n-gram），不是真正的神经语义 embedding；
- 完全离线、零费用、确定性可复现，适合测试与演示；
- 未来可替换为 BGE / OpenAI 等真实 embedding Provider（同 EmbeddingProvider 接口）。
"""

import hashlib
import math
import re
from typing import Iterator

from app.embeddings.base import EmbeddingProvider

# 模型/算法版本：向量语义随此字符串变化（持久化到 ChunkEmbedding.model）
MODEL_NAME = "local-hash-v1"

# 英文/数字 token（ASCII 小写归一）
_EN_WORD_RE = re.compile(r"[a-z0-9]+")
# CJK 统一表意文字区间（含扩展 A）：连续段内做 n-gram
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]+")


def _tokens(text: str) -> Iterator[str]:
    """确定性 token 序列：英文词（小写）+ 中文 unigram/bigram。"""
    lower = text.lower()
    pos = 0
    length = len(lower)
    while pos < length:
        match = _EN_WORD_RE.match(lower, pos)
        if match:
            yield match.group(0)
            pos = match.end()
            continue
        match = _CJK_RE.match(lower, pos)
        if match:
            segment = match.group(0)
            # unigram + bigram（滑动窗口）；单个汉字只出 unigram
            for i, char in enumerate(segment):
                yield char
                if i + 1 < len(segment):
                    yield segment[i : i + 2]
            pos = match.end()
            continue
        pos += 1


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return vector  # 零向量保持原样（调用方会拒绝空白文本；防御路径）
    return [value / norm for value in vector]


class LocalHashEmbeddingProvider(EmbeddingProvider):
    """确定性 feature hashing 词汇向量（零网络检索基线，非语义模型）。"""

    def __init__(self, dimension: int | None = None):
        from app.core.config import settings as _settings

        self._dimension = dimension or _settings.embedding_dimension

    @property
    def provider_name(self) -> str:
        return "local-hash"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return MODEL_NAME

    def capabilities(self) -> dict:
        return {
            "semantic": False,
            "network": False,
            "deterministic": True,
            "description": (
                "local-hash: lightweight lexical-vector retrieval baseline "
                "(feature hashing), NOT a neural semantic embedding"
            ),
        }

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self._dimension
            for token in _tokens(text):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                index = int.from_bytes(digest[:4], "big") % self._dimension
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign
            vectors.append(_l2_normalize(vector))
        return vectors
