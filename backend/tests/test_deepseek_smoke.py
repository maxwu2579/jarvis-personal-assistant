"""真实 DeepSeek 冒烟测试（显式手动运行；默认全量 pytest 一律跳过，不消耗余额）。

运行条件（必须同时满足）：
- 环境变量 RUN_DEEPSEEK_SMOKE=1；
- LLM_PROVIDER=deepseek（来自用户 .env / 环境变量）；
- LLM_API_KEY 非空。

未满足时整体 skip —— 跳过 ≠ 失败，也不是测试遗漏。

只发最少请求（每个测试恰好一次网络调用，无任何重试）：
1. 一次普通短对话（验证 HTTP 成功、模型名、非空回复）；
2. 一次结构化 create_task_draft 建议（JSON Mode，返回内容经 Pydantic 二次验证）。

诚实约定：
- 不打印 Key、不打印完整 Prompt / 回复内容；
- 若 DeepSeek JSON Mode 返回空内容，明确失败，不假装通过、不无限重试；
- 直接调用 Provider，不经过 HTTP / 数据库，零副作用。
"""

import os

import pytest

from app.core.config import settings
from app.llm.factory import get_llm_provider
from app.llm.prompts import build_proposal_system_prompt, build_proposal_user_prompt
from app.schemas.proposal import TaskProposal

_SMOKE_ENABLED = os.environ.get("RUN_DEEPSEEK_SMOKE") == "1"
_SMOKE_PROVIDER_OK = settings.llm_provider.strip().lower() == "deepseek"
_SMOKE_KEY_PRESENT = bool(settings.llm_api_key)
_SMOKE_READY = _SMOKE_ENABLED and _SMOKE_PROVIDER_OK and _SMOKE_KEY_PRESENT

_SKIP_REASON = (
    "真实冒烟需要同时满足：RUN_DEEPSEEK_SMOKE=1、LLM_PROVIDER=deepseek、"
    "LLM_API_KEY 非空。当前未满足 —— skipped（不是失败）"
)

pytestmark = pytest.mark.skipif(not _SMOKE_READY, reason=_SKIP_REASON)


def test_smoke_plain_chat():
    provider = get_llm_provider()
    # 一次普通短对话（无 response_schema → 无 response_format，与 OpenAI 行为一致）
    response = provider.generate(
        [{"role": "user", "content": "Reply with exactly two words: all good."}]
    )
    assert response.model, "响应缺少模型名"
    assert response.text.strip(), "DeepSeek 返回空内容：明确失败，不做任何重试"
    print(
        f"[smoke] plain chat ok: model={response.model} "
        f"latency_ms={response.latency_ms} prompt_tokens={response.prompt_tokens} "
        f"completion_tokens={response.completion_tokens}"
    )


def test_smoke_structured_proposal():
    provider = get_llm_provider()
    system = build_proposal_system_prompt(
        current_date="2026-08-16", user_timezone="Asia/Shanghai"
    )
    user = build_proposal_user_prompt("请帮我安排在2026年8月20日下午5点前完成JARVIS报告")
    # 一次结构化建议（JSON Mode；请求只发一次，无重试）
    response = provider.generate(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_schema=TaskProposal,
    )
    assert response.text.strip(), (
        "DeepSeek JSON Mode 返回空内容：明确失败，不假装通过，也不重试"
    )
    proposal = TaskProposal.model_validate_json(response.text)
    assert proposal.action == "create_task_draft"
    print(
        f"[smoke] structured ok: model={response.model} "
        f"latency_ms={response.latency_ms} prompt_tokens={response.prompt_tokens} "
        f"completion_tokens={response.completion_tokens} action={proposal.action}"
    )
