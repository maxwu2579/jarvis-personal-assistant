"""Phase 6F 验证测试（离线部分）。

Phase 6F = Real Integration and Final Verification（不新增产品功能）。
本文件覆盖离线 Gate 1.2 / 1.3：

- Gate 1.2 错误信封（规格十）：用【实际响应】断言
  {"error": {"code", "message", "details", "correlation_id"}} 结构，
  以及 X-Request-ID 的透传 / 缺失生成 / 响应头与 body correlation_id
  完全一致 / 内部异常零泄漏（traceback、类名、SQL、token、密钥）；
- Gate 1.3 Reminder 旧接口兼容：既有 HTTP 状态码不变、
  detail.error 读取方式仍可用（Reminder domain/service/worker 行为
  零改动，由 tests/test_reminders.py 全绿承担证明）。

真实 PostgreSQL Gate 见 tests/test_phase6f_pg.py（显式门控）。
"""

import uuid

from app.api import executions as executions_api
from app.services.execution_registry import EXECUTION_TYPES, ExecutionTypeSpec

# ---- 泄漏探测标记：真实异常中出现的内部细节（类名/SQL/密钥样式/traceback） ----
_LEAK_MARKERS = (
    "RuntimeError",
    "Traceback",
    "sk-super-secret-",
    "SELECT",
    "users",
)


# =====================================================================
# Gate 1.2：错误信封 + correlation 实际响应断言（规格十）
# =====================================================================


def test_error_envelope_live_valid_request_id_passthrough(client, worker_env, monkeypatch):
    """合法 X-Request-ID 透传：响应头原样回显，body correlation_id 一致。"""
    cid = "client-sent-request-id-0001"
    res = client.post(
        "/api/executions",
        json={"execution_type": "system.ping", "payload": {}},
        headers={"Idempotency-Key": "f6-key-0001", "X-Request-ID": cid},
    )
    assert res.status_code == 201
    assert res.headers["X-Request-ID"] == cid
    assert res.json()["execution"]["correlation_id"] == cid


def test_error_envelope_live_missing_request_id_generated(client):
    """缺失 X-Request-ID → 服务端生成（uuid4 hex），响应头与 body 一致。"""
    res = client.post(
        "/api/executions",
        json={"execution_type": "system.ping", "payload": {}},
        headers={"Idempotency-Key": "f6-key-0002"},
    )
    assert res.status_code == 201
    cid = res.headers.get("X-Request-ID")
    assert cid, "缺失请求 ID 时必须生成并回显响应头"
    # uuid4().hex == 32 位小写 hex
    uuid.UUID(cid)  # 格式非法即抛 ValueError
    assert len(cid) == 32
    assert res.json()["execution"]["correlation_id"] == cid


def test_error_envelope_live_conflict_structure_and_header(client, monkeypatch):
    """错误响应的真实结构：body 恰为 error 四键；响应头与 body 完全一致。"""
    # 注册可携带 echo 字段的测试类型（monkeypatch 自动恢复）
    monkeypatch.setitem(
        EXECUTION_TYPES,
        "e6.ping",
        ExecutionTypeSpec(
            name="e6.ping",
            payload_schema_version=1,
            payload_allowed_fields=frozenset({"echo"}),
            max_attempts=2,
            timeout_seconds=None,
            retry_backoff_base_seconds=2.0,
            retry_backoff_factor=2.0,
            steps=(),
        ),
    )
    key = "f6-key-0003"
    payload_a = {"echo": "a"}
    payload_b = {"echo": "b"}
    client.post(
        "/api/executions",
        json={"execution_type": "e6.ping", "payload": payload_a},
        headers={"Idempotency-Key": key, "X-Request-ID": "cid-0003"},
    )
    res = client.post(
        "/api/executions",
        json={"execution_type": "e6.ping", "payload": payload_b},
        headers={"Idempotency-Key": key, "X-Request-ID": "cid-0003"},
    )
    assert res.status_code == 409
    body = res.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {
        "code", "message", "details", "correlation_id"
    }
    assert body["error"]["code"] == "EXEC_IDEMPOTENCY_CONFLICT"
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)
    assert body["error"]["correlation_id"] == "cid-0003"
    assert res.headers["X-Request-ID"] == body["error"]["correlation_id"]


def test_error_envelope_live_internal_exception_no_leakage(client, worker_env, monkeypatch):
    """内部异常：客户端只收到稳定 500；不泄露 traceback/类名/SQL/token/密钥。"""
    secret = "sk-super-secret-6f-token"
    sql_fragment = "SELECT * FROM users"

    def _explode(*args, **kwargs):
        raise RuntimeError(f"boom {secret} {sql_fragment}")

    monkeypatch.setattr(executions_api, "create_execution", _explode)
    res = client.post(
        "/api/executions",
        json={"execution_type": "system.ping", "payload": {}},
        headers={"Idempotency-Key": "f6-key-0004"},
    )
    assert res.status_code == 500
    body = res.json()
    assert body == {
        "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}
    }
    # 响应文本全量扫描：任何内部细节都不应出现在客户端视野
    raw = res.text
    for marker in _LEAK_MARKERS + (secret, sql_fragment):
        assert marker not in raw, f"内部异常泄漏标记 {marker!r} 出现在响应中"
    assert res.headers.get("X-Request-ID"), "500 响应也必须带 correlation 头"


# =====================================================================
# Gate 1.3：Reminder 旧接口兼容（状态码不变 + detail.error 可读）
# =====================================================================


def test_reminder_compat_404_status_and_detail_error(client):
    """既有 404 状态码与 detail.error 读取方式不变。"""
    res = client.get("/api/reminders/999999")
    assert res.status_code == 404
    body = res.json()
    assert body["detail"]["error"]["code"] == "REMINDER_NOT_FOUND"
    assert "999999" in body["detail"]["error"]["message"]


def test_reminder_compat_422_status_and_detail_error(client):
    """既有 422 状态码与 detail.error 读取方式不变（无效 status 过滤）。"""
    res = client.get("/api/reminders?status=NOT_A_REAL_STATUS")
    assert res.status_code == 422
    body = res.json()
    assert body["detail"]["error"]["code"] == "VALIDATION_ERROR"


def test_reminder_compat_ok_paths_unchanged(client):
    """既有成功路径不受影响：列表 200 空数组、通知 200、未读数 0。"""
    res = client.get("/api/reminders")
    assert res.status_code == 200
    assert res.json() == []
    res = client.get("/api/notifications")
    assert res.status_code == 200
    assert res.json() == []
    res = client.get("/api/notifications/unread-count")
    assert res.status_code == 200
    assert res.json()["count"] == 0
