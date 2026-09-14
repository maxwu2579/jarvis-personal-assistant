"""FakeLLMProvider：本地模拟，用于自动化测试与无 Key 演示。

绝不发起任何网络请求。可注入错误 / 延迟以测试失败路径。
结构化输出模拟：
- reply_json：返回该对象的 JSON 序列化（合法结构、缺失/多余字段、错误 action 等均可注入）；
- reply_raw：完全控制原始回复文本（非 JSON、任意内容、Markdown 围栏均可注入）；
- 优先级：reply_raw > reply_json > reply。

check_messages（Phase 4C 扩展）：每次调用前对收到的消息执行断言回调（如
「system 消息不含文档正文」「上下文只含本次检索的证据」），失败直接抛异常，
用于 prompt-injection 场景的确定性验证——Fake 本身不可被提示词“说服”。
"""

import json
import time
from typing import Any, Callable

from pydantic import BaseModel

from app.llm.base import LLMProvider, LLMResponse, ProviderMessage


class FakeLLMProvider(LLMProvider):
    def __init__(
        self,
        *,
        model: str = "fake-model",
        reply: str = "This is a fake reply.",
        reply_json: dict[str, Any] | None = None,
        reply_raw: str | None = None,
        error: Exception | None = None,
        delay_seconds: float = 0.0,
        check_messages: Callable[[list[ProviderMessage]], None] | None = None,
    ):
        self.model = model
        self.reply = reply
        self.reply_json = reply_json
        self.reply_raw = reply_raw
        self.error = error
        self.delay_seconds = delay_seconds
        self.check_messages = check_messages
        # 每次调用的入参快照，供测试断言「Provider 收到了正确上下文 / schema」
        self.calls: list[list[ProviderMessage]] = []
        self.schemas_received: list[type[BaseModel] | None] = []

    def generate(
        self,
        messages: list[ProviderMessage],
        *,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        if self.delay_seconds > 0:
            time.sleep(self.delay_seconds)
        if self.error is not None:
            raise self.error

        self.calls.append([dict(m) for m in messages])
        self.schemas_received.append(response_schema)
        if self.check_messages is not None:
            self.check_messages(messages)

        if self.reply_raw is not None:
            text = self.reply_raw
        elif self.reply_json is not None:
            text = json.dumps(self.reply_json, ensure_ascii=False)
        else:
            text = self.reply

        prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in messages)
        completion_tokens = len(text.split())
        return LLMResponse(
            text=text,
            model=self.model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=42,
        )
