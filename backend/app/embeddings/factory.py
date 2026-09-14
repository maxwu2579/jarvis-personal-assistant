"""EmbeddingProvider 工厂：按配置名创建 provider。

支持：
- local-hash（默认）：零网络词汇向量（明确不是神经语义模型）；
- fake：固定测试向量（pgvector 集成测试用，绝不生产静默启用；
  生产配置选它 → config validator 启动即拒绝）；
- openai：OpenAI 兼容语义 Embedding 的配置名（Phase 5C 起 provider 自身
  标识 provider_name="openai-compatible" 并持久化；EMBEDDING_MODEL 必填、
  Key 只能从环境注入；普通测试完全 mock；真实联网由
  RUN_LIVE_EMBEDDING_TESTS=1 单独门控）。

未知 provider 在工厂层给出清晰配置错误（config validator 已在启动时拒绝），
绝不静默降级或回退（外部 provider 失败 → 异常传播 → 索引事务整体回滚）。
"""

from app.embeddings.base import EmbeddingError, EmbeddingProvider
from app.embeddings.fake_provider import DeterministicFakeEmbeddingProvider
from app.embeddings.local_hash_provider import LocalHashEmbeddingProvider

_PROVIDER_FACTORIES: dict[str, type[EmbeddingProvider]] = {
    "local-hash": LocalHashEmbeddingProvider,
    "fake": DeterministicFakeEmbeddingProvider,
}


def create_embedding_provider(
    name: str | None = None, dimension: int | None = None
) -> EmbeddingProvider:
    """创建 provider；name 缺省时使用配置（settings.embedding_provider）。

    openai 需要额外配置参数（model/api_key），在此按配置完整构造；
    构造失败（缺 Key / 未知模型 / 维度不合法）→ EmbeddingError，
    启动即失败，绝不静默回退。
    """
    from app.core.config import settings

    provider_name = name or settings.embedding_provider
    if provider_name == "openai":
        from app.embeddings.openai_provider import OpenAIEmbeddingProvider

        return OpenAIEmbeddingProvider(dimension=dimension)
    factory = _PROVIDER_FACTORIES.get(provider_name)
    if factory is None:
        known = ", ".join(sorted(set(_PROVIDER_FACTORIES) | {"openai"}))
        raise EmbeddingError(
            "UNKNOWN_EMBEDDING_PROVIDER",
            f"unknown embedding provider {provider_name!r} (available: {known})",
        )
    return factory(dimension)
