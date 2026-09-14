"""LLM 网关：与具体模型供应商解耦的抽象层。

约定：
- Provider 只负责「消息 → 文本/元数据」，不直接执行任何副作用；
- 调用方（chat_service）负责持久化与编排；
- 所有错误以本包定义的 LLM*Error 抛出，由 API 层映射为统一错误结构；
- 任何 Provider 实现都不得写入 API Key。
"""

from app.llm.base import (
    LLMError,
    LLMNotConfiguredError,
    LLMProvider,
    LLMResponse,
    LLMTimeoutError,
    LLMUpstreamError,
)
from app.llm.factory import get_llm_provider

__all__ = [
    "LLMError",
    "LLMNotConfiguredError",
    "LLMProvider",
    "LLMResponse",
    "LLMTimeoutError",
    "LLMUpstreamError",
    "get_llm_provider",
]
