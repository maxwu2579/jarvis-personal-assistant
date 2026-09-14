"""DeterministicFakeEmbeddingProvider：固定维度、零网络测试向量（Phase 5B）。

用途：
- pgvector 真实集成测试：需要确定性的固定维度向量来验证「索引服务写入的
  向量 == provider 输出」的往返一致性与维度契约（LocalHash 对随机语料的
  余弦噪声会干扰精确断言，fake 让结果可精确复现）；
- 单元测试的 provider 批量/维度/数值契约验证。

诚实边界：
- 这是测试替身，**不是**真实语义 Embedding，也**不是** local-hash；
- 输出是内容哈希派生的稀疏方向向量：相同输入 → 完全相同向量，
  不同输入 → 几乎正交（碰撞概率极低），最近邻期望可精确构造；
- 绝不通过生产配置启用：Phase 5C 起 config validator 对
  embedding_provider=fake **fail closed（启动即拒绝）**；测试只通过
  直接构造实例注入（RetrievalIndexService(db, provider)），默认配置
  仍是 local-hash。
"""

import hashlib
import math

from app.embeddings.base import EmbeddingProvider

_MODEL_NAME = "fake-deterministic-v1"


class DeterministicFakeEmbeddingProvider(EmbeddingProvider):
    """确定性测试向量：内容 sha256 → (归一化稀疏方向向量)。

    性质：零网络、零随机、相同输入恒同输出；不同输入的向量近似正交，
    任意两个不同文本的余弦期望 ≈ 0，精确最近邻可在测试中直接构造。
    """

    def __init__(self, dimension: int = 384):
        if dimension < 1:
            raise ValueError("dimension must be >= 1")
        self._dimension = dimension

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return _MODEL_NAME

    def capabilities(self) -> dict:
        return {
            "semantic": False,
            "network": False,
            "deterministic": True,
            "description": "fake: deterministic test-only embedding provider",
        }

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector = [0.0] * self._dimension
            axis = int.from_bytes(digest[:4], "big") % self._dimension
            second = int.from_bytes(digest[4:8], "big") % self._dimension
            vector[axis] = 1.0
            if second != axis:
                vector[second] = 0.5
            vectors.append(_l2_normalize(vector))
        return vectors


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:  # pragma: no cover - 稀疏向量不可能为零
        return vector
    return [v / norm for v in vector]
