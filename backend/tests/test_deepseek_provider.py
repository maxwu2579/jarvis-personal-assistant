"""DeepSeek Provider 离线测试：mock openai SDK，零网络、不依赖用户 .env、不消耗余额。

覆盖：
- factory 装配（大小写/空格/缺 Key/unknown 错误提示/默认端点/模型透传）；
- 请求参数（普通聊天不发 response_format；response_schema 非空时发 json_object
  而非 json_schema；消息中包含显式 JSON 指令；schema 摘要长度受控）；
- 结构化输出经后端 Pydantic 二次验证；空内容/非法 JSON/缺字段/多余字段/错误类型
  不落 Task、不伪造 ASSISTANT、保留 REJECTED 审计；
- SDK timeout / connection error / API error 映射为现有稳定错误；
- 日志与错误响应不出现 API Key、上游原始响应或模型原始敏感输出。
"""

import json
import logging
import sqlite3
import types

import pytest
from pydantic import create_model

import openai
from app.core.config import settings
from app.llm.base import LLMNotConfiguredError, LLMTimeoutError, LLMUpstreamError
from app.llm.deepseek_provider import DEEPSEEK_DEFAULT_BASE_URL, DeepSeekChatProvider
from app.llm.factory import get_llm_provider
from app.main import app
from app.schemas.proposal import TaskProposal

API = "/api/conversations"
PROPOSALS = "/task-proposals"

VALID_ARGS = {
    "title": "完成JARVIS报告",
    "description": None,
    "due_at": "2026-08-20T17:00:00+08:00",
}
VALID_PROPOSAL = {
    "action": "create_task_draft",
    "arguments": VALID_ARGS,
    "explanation": "根据用户明确给出的目标和截止时间生成任务草稿。",
}


# ---------- mock OpenAI SDK（类名与 openai SDK 一致，provider 按异常类型名映射） ----------


class APITimeoutError(Exception):
    """对应 openai.APITimeoutError。"""


class APIConnectionError(Exception):
    """对应 openai.APIConnectionError（连接失败映射为超时）。"""


class APIError(Exception):
    """对应其他 openai APIError（映射为上游错误）。"""


class _FakeChatCompletions:
    def __init__(self, owner):
        self.owner = owner

    def create(self, **kwargs):
        self.owner.requests.append(kwargs)
        outcome = self.owner.outcome
        if isinstance(outcome, Exception):
            raise outcome
        return _make_response(outcome)


class _FakeChat:
    def __init__(self, owner):
        self.completions = _FakeChatCompletions(owner)


class _FakeClient:
    """替换 openai.OpenAI：捕获构造参数与每次请求，绝不访问网络。"""

    instances: list["_FakeClient"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.requests: list[dict] = []
        self.outcome: dict | Exception = {"content": "This is a fake deepseek reply."}
        self.chat = _FakeChat(self)
        _FakeClient.instances.append(self)


def _make_response(outcome: dict) -> object:
    """把测试设定的响应体转换为 SDK 形状的对象（model / choices / usage）。"""
    usage = outcome.get("usage")
    return types.SimpleNamespace(
        model=outcome.get("model", "deepseek-chat"),
        choices=[
            types.SimpleNamespace(
                message=types.SimpleNamespace(content=outcome.get("content", ""))
            )
        ],
        usage=(
            types.SimpleNamespace(
                prompt_tokens=usage["prompt_tokens"],
                completion_tokens=usage["completion_tokens"],
            )
            if usage is not None
            else None
        ),
    )


@pytest.fixture()
def fake_sdk(monkeypatch):
    """把 openai.OpenAI 替换为 fake client（零网络），并清空跨测试残留。"""
    _FakeClient.instances.clear()
    monkeypatch.setattr(openai, "OpenAI", _FakeClient)
    return _FakeClient


def _latest_client() -> _FakeClient:
    return _FakeClient.instances[-1]


def _build_provider(
    *, model: str = "deepseek-chat", base_url: str | None = None, timeout_seconds: float = 30.0
) -> DeepSeekChatProvider:
    return DeepSeekChatProvider(
        api_key="sk-test-not-real",  # 仅传给被 mock 的 SDK，绝不落日志
        model=model,
        base_url=base_url,
        timeout_seconds=timeout_seconds,
    )


# ---------- factory 装配 ----------


def test_factory_selects_deepseek_provider(monkeypatch, fake_sdk):
    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_model", "deepseek-chat")
    monkeypatch.setattr(settings, "llm_base_url", "")

    provider = get_llm_provider()
    assert isinstance(provider, DeepSeekChatProvider)
    # 模型继续读 LLM_MODEL，不把 DeepSeek 绑定到 OpenAI 默认模型
    assert provider.model == "deepseek-chat"
    # 缺 LLM_BASE_URL 时安全默认官方端点
    assert _latest_client().kwargs["base_url"] == DEEPSEEK_DEFAULT_BASE_URL


def test_factory_provider_name_case_and_whitespace(monkeypatch, fake_sdk):
    monkeypatch.setattr(settings, "llm_provider", "  DeEpSeEk  ")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    assert isinstance(get_llm_provider(), DeepSeekChatProvider)


def test_factory_deepseek_missing_key_raises_not_configured(monkeypatch, fake_sdk):
    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "llm_api_key", "")
    with pytest.raises(LLMNotConfiguredError) as excinfo:
        get_llm_provider()
    assert "LLM_API_KEY" in str(excinfo.value)


def test_factory_unknown_provider_lists_supported(monkeypatch, fake_sdk):
    monkeypatch.setattr(settings, "llm_provider", "claude-nowhere")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    with pytest.raises(LLMNotConfiguredError) as excinfo:
        get_llm_provider()
    message = str(excinfo.value)
    for name in ("openai", "deepseek", "fake"):
        assert name in message


def test_factory_deepseek_honors_custom_base_url(monkeypatch, fake_sdk):
    monkeypatch.setattr(settings, "llm_provider", "deepseek")
    monkeypatch.setattr(settings, "llm_api_key", "sk-test")
    monkeypatch.setattr(settings, "llm_base_url", "https://gateway.example.com/v1")
    get_llm_provider()
    assert _latest_client().kwargs["base_url"] == "https://gateway.example.com/v1"


# ---------- 请求参数 ----------


def test_plain_chat_sends_no_response_format(fake_sdk):
    provider = _build_provider()
    client = _latest_client()
    provider.generate([{"role": "user", "content": "你好"}])
    request = client.requests[-1]
    assert "response_format" not in request
    assert request["model"] == "deepseek-chat"
    # 普通对话的消息原样透传，不注入 JSON 指令
    assert request["messages"] == [{"role": "user", "content": "你好"}]


def test_structured_uses_json_object_not_json_schema(fake_sdk):
    provider = _build_provider()
    client = _latest_client()
    provider.generate([{"role": "user", "content": "安排任务"}], response_schema=TaskProposal)
    request = client.requests[-1]
    assert request["response_format"] == {"type": "json_object"}
    assert "json_schema" not in json.dumps(request)


def test_structured_messages_include_explicit_json_instruction(fake_sdk):
    provider = _build_provider()
    client = _latest_client()
    provider.generate([{"role": "user", "content": "安排任务"}], response_schema=TaskProposal)
    messages = client.requests[-1]["messages"]
    # JSON Mode 指令在请求消息中（DeepSeek 要求 prompt 出现 json 字样）
    assert any("JSON" in m.get("content", "") for m in messages)
    # 指令消息携带 schema 摘要（字段名/类型/const），不含任何密钥
    instruction = messages[0]
    assert instruction["role"] == "system"
    # schema 摘要包含对象名与字段名（不含任何密钥与敏感值）
    for field in ("TaskProposal", "action", "arguments", "explanation"):
        assert field in instruction["content"]
    assert "sk-" not in instruction["content"]
    assert "api_key" not in json.dumps(instruction).lower()
    # 调用方原始消息全部保留且顺序不变
    assert messages[-1] == {"role": "user", "content": "安排任务"}
    assert len(messages) == 2


def test_structured_schema_summary_length_capped(fake_sdk):
    wide_schema = create_model(
        "WideSchema", **{f"field_{i:03d}": (int, 0) for i in range(150)}
    )
    provider = _build_provider()
    client = _latest_client()
    provider.generate([{"role": "user", "content": "x"}], response_schema=wide_schema)
    content = client.requests[-1]["messages"][0]["content"]
    assert "(truncated)" in content
    assert len(content) < 1500  # 固定指令文案 + 1000 字符截断摘要


def test_response_metadata_recorded(fake_sdk):
    provider = _build_provider()
    client = _latest_client()
    client.outcome = {
        "model": "deepseek-chat",
        "content": json.dumps(VALID_PROPOSAL, ensure_ascii=False),
        "usage": {"prompt_tokens": 120, "completion_tokens": 30},
    }
    response = provider.generate(
        [{"role": "user", "content": "安排任务"}], response_schema=TaskProposal
    )
    assert response.model == "deepseek-chat"
    assert response.prompt_tokens == 120
    assert response.completion_tokens == 30
    assert isinstance(response.latency_ms, int) and response.latency_ms >= 0
    assert json.loads(response.text)["action"] == "create_task_draft"


def test_single_api_call_no_retry(fake_sdk):
    """生成型调用不重试：一次失败只发一次请求（避免重复计费与重复副作用）。"""
    provider = _build_provider()
    client = _latest_client()
    client.outcome = APIError("boom")
    with pytest.raises(LLMUpstreamError):
        provider.generate([{"role": "user", "content": "hi"}])
    assert len(client.requests) == 1


# ---------- SDK 错误映射（稳定文案，不泄露上游细节与 Key） ----------


def test_sdk_timeout_maps_to_timeout_error(fake_sdk):
    provider = _build_provider(timeout_seconds=7.5)
    _latest_client().outcome = APITimeoutError("request timed out: sk-secret-detail")
    with pytest.raises(LLMTimeoutError) as excinfo:
        provider.generate([{"role": "user", "content": "hi"}])
    assert str(excinfo.value) == "LLM provider timed out after 7.5s"
    assert "sk-secret" not in str(excinfo.value)


def test_sdk_connection_error_maps_to_timeout_error(fake_sdk):
    provider = _build_provider()
    _latest_client().outcome = APIConnectionError("connection refused")
    with pytest.raises(LLMTimeoutError):
        provider.generate([{"role": "user", "content": "hi"}])
    assert len(_latest_client().requests) == 1


def test_sdk_api_error_maps_to_upstream_error_no_leak(fake_sdk):
    provider = _build_provider()
    _latest_client().outcome = APIError("secret upstream body: sk-abc123")
    with pytest.raises(LLMUpstreamError) as excinfo:
        provider.generate([{"role": "user", "content": "hi"}])
    assert str(excinfo.value) == "LLM provider returned an error"
    assert "secret" not in str(excinfo.value)
    assert "sk-abc123" not in str(excinfo.value)


def test_sdk_error_logs_no_key_no_raw_response(fake_sdk, caplog):
    provider = _build_provider()
    _latest_client().outcome = APIError("secret sk-abc123 upstream detail")
    with caplog.at_level(logging.WARNING, logger="app.llm.openai_provider"):
        with pytest.raises(LLMUpstreamError):
            provider.generate([{"role": "user", "content": "hi"}])
    assert "sk-abc123" not in caplog.text
    assert "secret" not in caplog.text


# ---------- HTTP 集成（真实 DeepSeek Provider + mock SDK，走完整服务编排） ----------


def _use_deepseek_provider(outcome: dict | Exception):
    """把 LLM 依赖替换为 DeepSeek Provider；outcome 为 mock SDK 的响应/异常。"""
    provider = _build_provider()
    fake_client = _latest_client()
    fake_client.outcome = outcome
    app.dependency_overrides[get_llm_provider] = lambda: provider
    return fake_client


def _make_conversation(client) -> int:
    resp = client.post(API, json={"title": "deepseek test"})
    assert resp.status_code == 201
    return resp.json()["id"]


def _propose(client, content="请帮我安排在2026年8月20日下午5点前完成JARVIS报告。"):
    return client.post(
        f"{API}/{_make_conversation(client)}{PROPOSALS}",
        json={"content": content, "timezone": "Asia/Shanghai"},
    )


def test_deepseek_valid_proposal_creates_draft(client, fake_sdk, tmp_path):
    _use_deepseek_provider({"content": json.dumps(VALID_PROPOSAL, ensure_ascii=False)})
    resp = _propose(client)
    assert resp.status_code == 201
    body = resp.json()
    # 结构化 JSON 通过后端 Pydantic 二次验证 → 创建 DRAFT，审计 SUCCEEDED
    assert body["created_task"]["status"] == "DRAFT"
    assert body["created_task"]["title"] == "完成JARVIS报告"
    assert body["created_task"]["due_at"] == "2026-08-20T09:00:00Z"  # +08:00 → UTC
    assert body["proposal"]["status"] == "SUCCEEDED"
    assert body["assistant_message"]["role"] == "ASSISTANT"
    assert body["usage"]["prompt_tokens"] is None  # mock 响应未带 usage


def test_deepseek_plain_chat_roundtrip(client, fake_sdk):
    fake_client = _use_deepseek_provider({"content": "DeepSeek 的回复"})
    cid = _make_conversation(client)
    resp = client.post(f"{API}/{cid}/messages", json={"content": "你好"})
    assert resp.status_code == 201
    assert resp.json()["assistant_message"]["content"] == "DeepSeek 的回复"
    assert resp.json()["assistant_message"]["model"] == "deepseek-chat"
    # 普通聊天不发送 response_format
    assert "response_format" not in fake_client.requests[-1]


@pytest.mark.parametrize(
    "outcome_content, reason",
    [
        ("", "空内容"),
        ("hello, I am not JSON", "非法 JSON"),
        (json.dumps({**VALID_PROPOSAL, "arguments": {**VALID_ARGS, "extra_field": 1}}), "多余字段"),
        (json.dumps({"action": "create_task_draft", "arguments": {}, "explanation": "x"}), "缺字段"),
        (json.dumps({**VALID_PROPOSAL, "arguments": {**VALID_ARGS, "title": 123}}), "错误类型"),
    ],
)
def test_deepseek_invalid_outputs_no_task(client, fake_sdk, tmp_path, outcome_content, reason):
    _use_deepseek_provider({"content": outcome_content})
    resp = _propose(client)
    assert resp.status_code == 502, f"{reason} 应被拒绝"
    assert resp.json()["error"]["code"] == "LLM_SCHEMA_ERROR"
    assert resp.json()["error"]["message"] == "LLM returned an invalid task proposal"
    if outcome_content:  # 空内容本身是合法子串，无法用 in 断言
        assert outcome_content not in resp.text  # 不泄露模型原始输出

    con = sqlite3.connect(tmp_path / "test_tasks.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0, f"{reason} 不应创建任务"
        assert con.execute("SELECT COUNT(*) FROM messages WHERE role='ASSISTANT'").fetchone()[0] == 0
        # REJECTED 审计保留，不绕过审计规则
        assert con.execute("SELECT COUNT(*) FROM task_proposals WHERE status='REJECTED'").fetchone()[0] == 1
    finally:
        con.close()


def test_deepseek_upstream_error_502_stable(client, fake_sdk):
    _use_deepseek_provider(APIError("secret sk-abc123 upstream detail"))
    resp = _propose(client)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "LLM_UPSTREAM_ERROR"
    assert "sk-abc123" not in resp.text
    assert "secret" not in resp.text
    assert "Traceback" not in resp.text
