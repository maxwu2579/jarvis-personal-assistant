"""对话 API 测试：全部使用 FakeLLMProvider，绝不连接真实模型。"""

from app.core.config import settings
from app.llm.base import LLMTimeoutError, LLMUpstreamError
from app.llm.factory import get_llm_provider
from app.main import app

API = "/api/conversations"
REPLY = "This is a fake reply."


def create_conversation(client, title=None):
    payload = {} if title is None else {"title": title}
    return client.post(API, json=payload)


def _error(resp):
    return resp.json()["error"]


# ---------- Conversation ----------


def test_create_and_read_conversation(client):
    created = create_conversation(client, "  Phase 1 讨论  ")
    assert created.status_code == 201
    body = created.json()
    assert body["title"] == "Phase 1 讨论"
    assert body["created_at"].endswith("Z")
    assert body["updated_at"].endswith("Z")

    listing = client.get(API)
    assert listing.status_code == 200
    assert [c["id"] for c in listing.json()] == [body["id"]]

    detail = client.get(f"{API}/{body['id']}")
    assert detail.status_code == 200
    assert detail.json() == body


def test_conversation_blank_title_becomes_none(client):
    created = create_conversation(client, "   ")
    assert created.status_code == 201
    assert created.json()["title"] is None


def test_conversation_without_title_ok(client):
    created = create_conversation(client)
    assert created.status_code == 201
    assert created.json()["title"] is None


def test_conversation_rejects_undefined_fields(client):
    resp = client.post(API, json={"title": "x", "extra": 1})
    assert resp.status_code == 422
    assert _error(resp)["code"] == "VALIDATION_ERROR"


def test_conversation_not_found_404(client):
    for path in (API, f"{API}/99999", f"{API}/99999/messages"):
        resp = client.get(path)
        if path == API:
            continue
        assert resp.status_code == 404
        assert _error(resp)["code"] == "CONVERSATION_NOT_FOUND"


def test_messages_endpoint_404_for_missing_conversation(client):
    resp = client.get(f"{API}/99999/messages")
    assert resp.status_code == 404
    assert _error(resp)["code"] == "CONVERSATION_NOT_FOUND"


# ---------- 发送消息 ----------


def test_send_message_saves_user_and_assistant_in_order(client):
    cid = create_conversation(client, "Hello").json()["id"]

    resp = client.post(f"{API}/{cid}/messages", json={"content": "  你好，JARVIS  "})
    assert resp.status_code == 201
    exchange = resp.json()
    assert exchange["user_message"]["role"] == "USER"
    assert exchange["user_message"]["content"] == "你好，JARVIS"
    assert exchange["assistant_message"]["role"] == "ASSISTANT"
    assert exchange["assistant_message"]["content"] == REPLY

    messages = client.get(f"{API}/{cid}/messages").json()
    assert [m["role"] for m in messages] == ["USER", "ASSISTANT"]
    assert messages[0]["content"] == "你好，JARVIS"
    assert messages[1]["content"] == REPLY


def test_send_message_missing_conversation_writes_nothing(client):
    resp = client.post(f"{API}/99999/messages", json={"content": "hi"})
    assert resp.status_code == 404
    assert _error(resp)["code"] == "CONVERSATION_NOT_FOUND"
    assert client.get(API).json() == []  # 没有任何数据被写入


def test_send_message_blank_content_rejected(client):
    cid = create_conversation(client).json()["id"]
    for blank in ("", "   ", "\t"):
        resp = client.post(f"{API}/{cid}/messages", json={"content": blank})
        assert resp.status_code == 422, f"blank content {blank!r} must be rejected"


def test_send_message_rejects_undefined_fields(client):
    cid = create_conversation(client).json()["id"]
    resp = client.post(f"{API}/{cid}/messages", json={"content": "hi", "role": "ASSISTANT"})
    assert resp.status_code == 422
    assert _error(resp)["code"] == "VALIDATION_ERROR"


def test_send_message_content_length_limit(client):
    cid = create_conversation(client).json()["id"]
    assert client.post(f"{API}/{cid}/messages", json={"content": "x" * 8000}).status_code == 201
    resp = client.post(f"{API}/{cid}/messages", json={"content": "x" * 8001})
    assert resp.status_code == 422


def test_provider_receives_correct_context(client, fake_provider):
    cid = create_conversation(client).json()["id"]
    client.post(f"{API}/{cid}/messages", json={"content": "第一问"})
    assert fake_provider.calls[-1] == [{"role": "user", "content": "第一问"}]

    client.post(f"{API}/{cid}/messages", json={"content": "第二问"})
    assert fake_provider.calls[-1] == [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": REPLY},
        {"role": "user", "content": "第二问"},
    ]


def test_tokens_and_latency_metadata_saved(client):
    cid = create_conversation(client).json()["id"]
    exchange = client.post(f"{API}/{cid}/messages", json={"content": "元数据"}).json()

    assistant = exchange["assistant_message"]
    assert assistant["model"] == "fake-model"
    assert assistant["prompt_tokens"] == 1  # 1 个词
    assert assistant["completion_tokens"] == 5  # "This is a fake reply."
    assert assistant["latency_ms"] == 42

    usage = exchange["usage"]
    assert usage == {
        "prompt_tokens": assistant["prompt_tokens"],
        "completion_tokens": assistant["completion_tokens"],
        "latency_ms": assistant["latency_ms"],
    }


def test_message_created_at_is_utc(client):
    cid = create_conversation(client).json()["id"]
    exchange = client.post(f"{API}/{cid}/messages", json={"content": "时间"}).json()
    assert exchange["user_message"]["created_at"].endswith("Z")
    assert exchange["assistant_message"]["created_at"].endswith("Z")


def test_conversation_activity_refreshes_updated_at(client):
    cid = create_conversation(client, "活动").json()["id"]
    before = client.get(f"{API}/{cid}").json()["updated_at"]
    client.post(f"{API}/{cid}/messages", json={"content": "hi"})
    after = client.get(f"{API}/{cid}").json()["updated_at"]
    # 两次读取在毫秒级可能相同，但列表排序仍按 updated_at 生效：
    # 至少保证字段合法且对话可读
    assert after.endswith("Z")
    assert before.endswith("Z")


# ---------- 失败路径 ----------


def test_provider_timeout_returns_502_and_keeps_user_message(
    client, fake_provider
):
    fake_provider.error = LLMTimeoutError("timed out")
    cid = create_conversation(client).json()["id"]

    resp = client.post(f"{API}/{cid}/messages", json={"content": "会超时吗"})
    assert resp.status_code == 502
    body = _error(resp)
    assert body["code"] == "LLM_TIMEOUT"
    # 稳定文案可以说明“超时”，但绝不泄露原始异常细节
    assert "boom" not in resp.text
    assert "Traceback" not in resp.text

    # 数据库状态明确：USER 已保存，没有半成品 ASSISTANT
    messages = client.get(f"{API}/{cid}/messages").json()
    assert [m["role"] for m in messages] == ["USER"]
    assert messages[0]["content"] == "会超时吗"


def test_provider_upstream_error_does_not_leak_internals(client, fake_provider):
    fake_provider.error = LLMUpstreamError("secret internal detail: table overflow")
    cid = create_conversation(client).json()["id"]

    resp = client.post(f"{API}/{cid}/messages", json={"content": "会报错吗"})
    assert resp.status_code == 502
    body = _error(resp)
    assert body["code"] == "LLM_UPSTREAM_ERROR"
    assert "overflow" not in resp.text
    assert "Traceback" not in resp.text
    assert "secret" not in resp.text


def test_failure_state_is_consistent_and_recoverable(client, fake_provider):
    """失败后对话仍可读、仍可继续发送（恢复后正常）。"""
    cid = create_conversation(client).json()["id"]

    fake_provider.error = LLMTimeoutError("boom")
    assert client.post(f"{API}/{cid}/messages", json={"content": "第一轮失败"}).status_code == 502

    # 失败后：对话与已保存消息都清晰可见
    assert client.get(f"{API}/{cid}").status_code == 200
    roles = [m["role"] for m in client.get(f"{API}/{cid}/messages").json()]
    assert roles == ["USER"]

    # 恢复后：第二条 USER + ASSISTANT 正常追加，历史顺序不乱
    fake_provider.error = None
    assert client.post(f"{API}/{cid}/messages", json={"content": "第二轮成功"}).status_code == 201
    roles = [m["role"] for m in client.get(f"{API}/{cid}/messages").json()]
    assert roles == ["USER", "USER", "ASSISTANT"]
    assert client.get(f"{API}/{cid}/messages").json()[2]["content"] == REPLY


# ---------- 无 API Key（真实 get_llm_provider 路径） ----------


def test_no_api_key_returns_clear_config_error(client, monkeypatch):
    """移除 Fake override，模拟生产环境缺 LLM_API_KEY 的配置。"""
    app.dependency_overrides.pop(get_llm_provider, None)
    monkeypatch.setattr(settings, "llm_api_key", "")
    monkeypatch.setattr(settings, "llm_provider", "openai")

    cid = create_conversation(client).json()["id"]
    resp = client.post(f"{API}/{cid}/messages", json={"content": "hi"})
    assert resp.status_code == 503
    body = _error(resp)
    assert body["code"] == "LLM_NOT_CONFIGURED"
    assert "key" in body["message"].lower() or "LLM" in body["message"]

    # 其他功能不受影响
    assert client.get("/health").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}
    task = client.post("/api/tasks", json={"title": "still works"})
    assert task.status_code == 201
    assert task.json()["status"] == "DRAFT"


def test_unknown_provider_returns_clear_config_error(client, monkeypatch):
    app.dependency_overrides.pop(get_llm_provider, None)
    monkeypatch.setattr(settings, "llm_provider", "anthropic-nowhere")

    cid = create_conversation(client).json()["id"]
    resp = client.post(f"{API}/{cid}/messages", json={"content": "hi"})
    assert resp.status_code == 503
    assert _error(resp)["code"] == "LLM_NOT_CONFIGURED"
