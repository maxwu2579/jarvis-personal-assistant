"""Task API 测试（真实 FastAPI/TestClient 全栈测试）。

覆盖：创建、列表/详情、404、正常/非法状态转换、时区校验、输入安全、
UTC 输出、health、开发库隔离。
"""

import sqlite3
from pathlib import Path

from sqlalchemy.engine import make_url

from app.core.config import settings

API = "/api/tasks"


def create(client, title="Buy milk", **extra):
    payload = {"title": title, **extra}
    return client.post(API, json=payload)


def _error(resp):
    return resp.json()["error"]


def _assert_invalid(resp):
    assert resp.status_code == 409
    assert _error(resp)["code"] == "INVALID_TRANSITION"


# ---------- 创建 ----------


def test_create_task_always_draft(client):
    resp = create(client, "Prepare slides", description="Q3 review deck")
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "DRAFT"
    assert body["title"] == "Prepare slides"
    assert body["description"] == "Q3 review deck"
    # 所有时间输出必须明确为 UTC（Z 结尾），绝不能是无时区字符串
    for field in ("created_at", "updated_at"):
        assert body[field].endswith("Z"), f"{field} must be UTC: {body[field]}"


def test_create_task_without_title_rejected(client):
    resp = client.post(API, json={"title": ""})
    assert resp.status_code == 422
    assert _error(resp)["code"] == "VALIDATION_ERROR"


def test_create_task_with_blank_title_rejected(client):
    """去除首尾空格后为空 → 422。"""
    for blank in ("   ", "\t", " \t  "):
        resp = create(client, blank)
        assert resp.status_code == 422, f"blank title {blank!r} must be rejected"
        assert "blank" in _error(resp)["message"]


def test_create_task_title_is_stripped(client):
    resp = create(client, "  买牛奶  ")
    assert resp.status_code == 201
    assert resp.json()["title"] == "买牛奶"


def test_create_task_length_limits(client):
    assert create(client, "x" * 200).status_code == 201
    assert create(client, "x" * 201).status_code == 422
    assert create(client, "desc", description="x" * 2000).status_code == 201
    assert create(client, "desc", description="x" * 2001).status_code == 422


def test_create_cannot_inject_protected_fields(client):
    """id / status / created_at / updated_at 一律禁止传入。"""
    for extra in (
        {"status": "DONE"},
        {"id": 5},
        {"created_at": "2020-01-01T00:00:00Z"},
        {"updated_at": "2020-01-01T00:00:00Z"},
    ):
        resp = client.post(API, json={"title": "Sneaky", **extra})
        assert resp.status_code == 422, f"field {extra} must be rejected"
        assert _error(resp)["code"] == "VALIDATION_ERROR"


def test_create_rejects_undefined_fields(client):
    """未定义字段必须 422，不能静默忽略。"""
    resp = client.post(API, json={"title": "x", "foo": 1, "bar": "baz"})
    assert resp.status_code == 422
    assert _error(resp)["code"] == "VALIDATION_ERROR"


def test_create_task_with_naive_due_at_rejected(client):
    resp = create(client, "Naive", due_at="2026-08-10T09:00:00")
    assert resp.status_code == 422
    body = _error(resp)
    assert body["code"] == "VALIDATION_ERROR"
    assert "timezone" in body["message"]


def test_create_task_with_tz_due_at_converted_to_utc(client):
    resp = create(client, "Tz", due_at="2026-08-10T09:00:00+08:00")
    assert resp.status_code == 201
    body = resp.json()
    assert body["due_at"] == "2026-08-10T01:00:00Z"
    assert body["due_at"].endswith("Z")


def test_all_output_times_are_utc(client):
    """写入 +08:00，API 返回的 due_at/created_at/updated_at 全部为 UTC。"""
    resp = create(client, "Utc check", due_at="2026-08-10T09:00:00+08:00")
    body = resp.json()
    assert body["due_at"] == "2026-08-10T01:00:00Z"
    assert body["created_at"].endswith("Z")
    assert body["updated_at"].endswith("Z")
    assert "+08:00" not in body["due_at"]


# ---------- 读取与持久化 ----------


def test_list_and_get_persist_created_task(client):
    a = create(client, "Task A").json()
    b = create(client, "Task B", due_at="2026-08-10T09:00:00+08:00").json()

    listing = client.get(API)
    assert listing.status_code == 200
    ids = [t["id"] for t in listing.json()]
    assert set(ids) == {a["id"], b["id"]}

    single = client.get(f"{API}/{a['id']}")
    assert single.status_code == 200
    assert single.json() == a

    single_b = client.get(f"{API}/{b['id']}").json()
    assert single_b["due_at"] == "2026-08-10T01:00:00Z"


def test_get_missing_task_returns_404(client):
    resp = client.get(f"{API}/99999")
    assert resp.status_code == 404
    assert _error(resp)["code"] == "TASK_NOT_FOUND"
    assert "99999" in _error(resp)["message"]


def test_list_order_newest_first(client):
    a = create(client, "First").json()
    b = create(client, "Second").json()
    ids = [t["id"] for t in client.get(API).json()]
    assert ids == [b["id"], a["id"]]


# ---------- 正常状态转换 ----------


def test_confirm_then_complete_flow(client):
    task = create(client, "Normal flow").json()
    assert task["status"] == "DRAFT"

    confirmed = client.post(f"{API}/{task['id']}/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "CONFIRMED"

    done = client.post(f"{API}/{task['id']}/complete")
    assert done.status_code == 200
    assert done.json()["status"] == "DONE"

    # 持久化确认：重新读取仍是 DONE
    assert client.get(f"{API}/{task['id']}").json()["status"] == "DONE"


def test_confirm_missing_task_404(client):
    assert client.post(f"{API}/99999/confirm").status_code == 404


# ---------- 非法状态转换 ----------


def test_draft_cannot_complete_directly(client):
    task = create(client, "Directly done").json()
    _assert_invalid(client.post(f"{API}/{task['id']}/complete"))


def test_done_cannot_confirm_again(client):
    task = create(client, "Already done").json()
    client.post(f"{API}/{task['id']}/confirm")
    client.post(f"{API}/{task['id']}/complete")
    _assert_invalid(client.post(f"{API}/{task['id']}/confirm"))


def test_confirm_twice_rejected(client):
    task = create(client, "Double confirm").json()
    client.post(f"{API}/{task['id']}/confirm")
    _assert_invalid(client.post(f"{API}/{task['id']}/confirm"))


def test_complete_twice_rejected(client):
    task = create(client, "Double complete").json()
    client.post(f"{API}/{task['id']}/confirm")
    client.post(f"{API}/{task['id']}/complete")
    _assert_invalid(client.post(f"{API}/{task['id']}/complete"))


def test_draft_cannot_postpone(client):
    task = create(client, "Draft postpone").json()
    resp = client.patch(f"{API}/{task['id']}/postpone", json={"due_at": "2026-08-11T00:00:00Z"})
    _assert_invalid(resp)


def test_done_cannot_postpone(client):
    task = create(client, "Done postpone").json()
    client.post(f"{API}/{task['id']}/confirm")
    client.post(f"{API}/{task['id']}/complete")
    resp = client.patch(f"{API}/{task['id']}/postpone", json={"due_at": "2026-08-11T00:00:00Z"})
    _assert_invalid(resp)


def test_error_message_is_stable_and_readable(client):
    task = create(client, "Msg check").json()
    client.post(f"{API}/{task['id']}/confirm")
    resp = client.post(f"{API}/{task['id']}/confirm")
    body = _error(resp)
    assert body["code"] == "INVALID_TRANSITION"
    assert "CONFIRMED" in body["message"]
    assert "confirm" in body["message"]


# ---------- 延期 ----------


def test_postpone_confirmed_task_updates_due_at(client):
    task = create(client, "Postpone me").json()
    client.post(f"{API}/{task['id']}/confirm")

    resp = client.patch(
        f"{API}/{task['id']}/postpone",
        json={"due_at": "2026-08-15T14:30:00+08:00"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "CONFIRMED"
    assert body["due_at"] == "2026-08-15T06:30:00Z"

    # 持久化验证
    assert client.get(f"{API}/{task['id']}").json()["due_at"] == "2026-08-15T06:30:00Z"


def test_postpone_requires_tz_aware_due_at(client):
    task = create(client, "Postpone naive").json()
    client.post(f"{API}/{task['id']}/confirm")
    resp = client.patch(f"{API}/{task['id']}/postpone", json={"due_at": "2026-08-11T00:00:00"})
    assert resp.status_code == 422
    assert "timezone" in _error(resp)["message"]


def test_postpone_rejects_extra_fields(client):
    """postpone 请求体只能有 due_at。"""
    task = create(client, "Extra fields").json()
    client.post(f"{API}/{task['id']}/confirm")
    resp = client.patch(
        f"{API}/{task['id']}/postpone",
        json={"due_at": "2026-08-11T00:00:00Z", "note": "not allowed"},
    )
    assert resp.status_code == 422
    assert _error(resp)["code"] == "VALIDATION_ERROR"


def test_postpone_after_deadline_allowed(client):
    """规则只约束状态，不约束时间大小。"""
    task = create(client, "Backdate").json()
    client.post(f"{API}/{task['id']}/confirm")
    resp = client.patch(f"{API}/{task['id']}/postpone", json={"due_at": "2020-01-01T00:00:00Z"})
    assert resp.status_code == 200
    assert resp.json()["due_at"] == "2020-01-01T00:00:00Z"


# ---------- 元信息 / 健康检查 ----------


def test_root_meta_endpoint(client):
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["docs"] == "/docs"
    assert body["openapi"] == "/openapi.json"


def test_docs_available(client):
    resp = client.get("/docs")
    assert resp.status_code == 200
    assert "swagger" in resp.text.lower()


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # 健康检查不得泄露配置
    low = resp.text.lower()
    for secretish in ("database_url", "password", "secret", "api_key"):
        assert secretish not in low


# ---------- 测试库与开发库隔离 ----------


def test_development_database_not_polluted(client):
    """测试数据只写入临时库，开发数据库文件不被写入任何任务行。"""
    assert create(client, "only in temp db").status_code == 201
    assert client.get(API).json()[0]["title"] == "only in temp db"

    url = make_url(settings.database_url)
    assert url.drivername == "sqlite"
    db_path = Path(url.database)
    if not db_path.exists():
        return  # 开发库尚未创建，同样满足“未被污染”

    con = sqlite3.connect(db_path)
    try:
        count = con.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        con.close()
    assert count == 0, "dev database must contain no task rows after tests"
