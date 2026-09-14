"""按环境变量构造 Provider。本模块不持有任何密钥。"""

import json

from app.core.config import settings
from app.llm.base import LLMNotConfiguredError, LLMProvider
from app.llm.deepseek_provider import DEEPSEEK_DEFAULT_BASE_URL, DeepSeekChatProvider
from app.llm.fake_provider import FakeLLMProvider
from app.llm.openai_provider import OpenAIChatProvider

# 错误提示与文档共用的受支持供应商列表
SUPPORTED_PROVIDERS = "openai, deepseek, fake"


def get_llm_provider() -> LLMProvider:
    """FastAPI 依赖：每次请求按当前配置构造 Provider（无状态、可安全 override）。"""
    provider_name = settings.llm_provider.strip().lower()

    if provider_name == "fake":
        reply_json = None
        if settings.llm_fake_reply_json.strip():
            try:
                reply_json = json.loads(settings.llm_fake_reply_json)
            except json.JSONDecodeError:
                raise LLMNotConfiguredError(
                    "LLM_FAKE_REPLY_JSON is not valid JSON"
                ) from None
        return FakeLLMProvider(reply_json=reply_json)

    if provider_name in ("openai", "deepseek"):
        if not settings.llm_api_key:
            raise LLMNotConfiguredError(
                f"LLM_API_KEY is not configured (LLM_PROVIDER={provider_name}). "
                "Set it in .env, or use LLM_PROVIDER=fake for local testing."
            )
        if provider_name == "openai":
            return OpenAIChatProvider(
                api_key=settings.llm_api_key,
                model=settings.llm_model,
                base_url=settings.llm_base_url.strip() or None,
                timeout_seconds=settings.llm_timeout_seconds,
            )
        # DeepSeek：LLM_BASE_URL 留空时安全默认官方端点；模型继续读 LLM_MODEL
        #（不绑定 OpenAI 默认模型，请自行配置 deepseek-chat / deepseek-reasoner）
        return DeepSeekChatProvider(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            base_url=settings.llm_base_url.strip() or DEEPSEEK_DEFAULT_BASE_URL,
            timeout_seconds=settings.llm_timeout_seconds,
        )

    raise LLMNotConfiguredError(
        f"Unknown LLM_PROVIDER {settings.llm_provider!r}. Supported: {SUPPORTED_PROVIDERS}"
    )
