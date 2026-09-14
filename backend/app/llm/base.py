"""LLM Provider 抽象接口与错误类型。"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

# 传给模型的消息格式（OpenAI 兼容）：{"role": "system|user|assistant|tool", "content": "..."}
ProviderMessage = dict[str, Any]


@dataclass(slots=True)
class LLMResponse:
    """一次模型调用的结果与基础元数据。"""

    text: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None


class LLMError(Exception):
    """LLM 网关错误基类。"""


class LLMNotConfiguredError(LLMError):
    """Provider 未配置（如缺少 LLM_API_KEY）。客户端只收到稳定文案。"""


class LLMTimeoutError(LLMError):
    """上游调用超时（受 LLM_TIMEOUT_SECONDS 约束）。"""


class LLMUpstreamError(LLMError):
    """上游返回错误或连接失败。不携带上游原始响应体。"""


class LLMProvider(ABC):
    """模型供应商抽象。实现必须是无状态、可测试、不产生数据库副作用的。"""

    @abstractmethod
    def generate(
        self,
        messages: list[ProviderMessage],
        *,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        """调用模型。

        messages: OpenAI 兼容的角色消息列表，由调用方构造完整上下文。
        response_schema: 为下一步 Structured Output 预留；本阶段实现可以忽略它。
        """
        raise NotImplementedError
