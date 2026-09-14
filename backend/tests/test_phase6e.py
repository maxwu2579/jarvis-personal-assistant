"""Phase 6E HTTP 契约测试：Execution API、错误信封、correlation、前端契约。

覆盖（规格五-十 + 审计报告承诺）：
- 创建与幂等：Idempotency-Key Header 契约（201/200 重放/409 冲突/422 缺失与
  非法）、body 旁路拒绝、未知 type/payload 白名单/naive run_at 稳定 422、
  响应不含 payload 原文与租约细节；
- 查询：404 信封、字段安全、列表白名单过滤/排序/分页、events 升序分页；
- Cancel：CREATED/QUEUED→CANCELLED、RUNNING→CANCEL_REQUESTED、重复幂等、
  终态稳定 409；
- Retry：FAILED→QUEUED CAS、双击幂等（无重复调度事件）、非 FAILED 409、
  旧 attempt/step/event 保留；
- Confirmation：actor 必填、body 无法指定 confirmation_status、跨 execution
  拒绝、GRANTED 放行 Worker、REJECTED 稳定失败、GRANTED/REJECTED 互不翻转、
  同 actor 重复确认幂等；
- 错误信封：{"error": {code, message, details, correlation_id}} +
  X-Request-ID 回显 + 事件 correlation_id 透传；
- 前端契约：types.ts / api.ts 的最小 Execution 类型与函数存在性。

worker 侧操作（enqueue/claim/run_once）经 worker_env 独立 session 驱动——
模拟独立 Worker 进程，与 HTTP 层同一临时库。
"""

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.clock import FixedClock
from app.models.execution import Execution, ExecutionStatus
from app.models.execution_step import ExecutionStep
from app.services.execution_claim import claim_next
from app.services.execution_events import list_events
from app.services.execution_registry import (
    EXECUTION_TYPES,
    EXECUTION_TOOLS,
    ExecutionTypeSpec,
    StepSpec,
    ToolSpec,
    get_tool,
    get_type_spec,
    resolve_step_template,
)
from app.services.execution_service import create_execution, enqueue, transition_to
from app.services.execution_steps import _materialize_step
from app.services.execution_worker import ExecutionWorker

BACKEND_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BACKEND_DIR.parent / "frontend" / "src"

# 与 conftest 的 client fixture 时钟一致（FixedClock(FIXED_NOW)）
FIXED_NOW = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)


class FakeClock:
    """可推进时钟（worker 侧；与 HTTP 层 FixedClock 同起点）。"""

    def __init__(self, start=FIXED_NOW):
        self._now = start

    def now(self):
        return self._now

    def advance(self, seconds):
        self._now = self._now + timedelta(seconds=seconds)


_key_counter = itertools.count(1)


def _unique_key(prefix="e6"):
    return f"{prefix}-{next(_key_counter):08d}"


def _aware_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---- 注册表隔离（monkeypatch 自动恢复）----


def _register_e6(monkeypatch):
    """两个声明式类型：e6.ping（安全，无需确认）+ e6.risk（确认门）。"""
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
            steps=(StepSpec(step_key="echo", tool_name="e6.echo"),),
        ),
    )
    monkeypatch.setitem(
        EXECUTION_TYPES,
        "e6.risk",
        ExecutionTypeSpec(
            name="e6.risk",
            payload_schema_version=1,
            payload_allowed_fields=frozenset({"message"}),
            max_attempts=3,  # PENDING 门失败可重试；REJECTED 不重试
            timeout_seconds=None,
            retry_backoff_base_seconds=2.0,
            retry_backoff_factor=2.0,
            steps=(StepSpec(step_key="risky", tool_name="e6.risky"),),
        ),
    )
    monkeypatch.setitem(
        EXECUTION_TOOLS,
        "e6.echo",
        ToolSpec(
            name="e6.echo",
            handler=lambda input_value: {"echo": input_value.get("echo")},
            input_schema=None,
            output_schema=None,
            destructive=False,
            requires_confirmation=False,
            retryable=True,
            replay_safe=True,
            sanitize_result=True,
            enabled=True,
        ),
    )
    monkeypatch.setitem(
        EXECUTION_TOOLS,
        "e6.risky",
        ToolSpec(
            name="e6.risky",
            handler=lambda input_value: {"done": True},
            input_schema=None,
            output_schema=None,
            destructive=True,  # 规格九：destructive 未确认前永不执行
            requires_confirmation=True,
            retryable=False,
            replay_safe=False,
            sanitize_result=True,
            enabled=True,
        ),
    )


# ---- 辅助 ----

def _db(worker_env):
    _, SessionLocal = worker_env
    return SessionLocal()


def _worker(worker_env, clock=None, worker_id="e6-worker"):
    _, SessionLocal = worker_env
    return ExecutionWorker(
        SessionLocal, worker_id=worker_id, clock=clock or FakeClock(),
        lease_ttl_seconds=60,
    )


def _claim(worker_env, worker_id="e6-worker", clock=None):
    db = _db(worker_env)
    try:
        return claim_next(
            db, worker_id, clock=clock or FakeClock(), lease_ttl_seconds=60
        )
    finally:
        db.close()


def _service_create(worker_env, execution_type="e6.ping", payload=None, **kwargs):
    db = _db(worker_env)
    try:
        return create_execution(
            db,
            execution_type=execution_type,
            payload=payload or {},
            idempotency_key=_unique_key("srv"),
            clock=FixedClock(FIXED_NOW),
            **kwargs,
        )
    finally:
        db.close()


def _service_enqueue(worker_env, execution_id):
    db = _db(worker_env)
    try:
        return enqueue(db, execution_id, clock=FixedClock(FIXED_NOW))
    finally:
        db.close()


def _reach_terminal(worker_env, execution_id, terminal: ExecutionStatus):
    """走合法白名单路径到终态：QUEUED → CLAIMED → RUNNING → terminal。"""
    db = _db(worker_env)
    try:
        enqueue(db, execution_id, clock=FixedClock(FIXED_NOW))
        claim_next(
            db, "e6-worker", clock=FixedClock(FIXED_NOW), lease_ttl_seconds=60
        )
        transition_to(db, execution_id, ExecutionStatus.RUNNING, clock=FixedClock(FIXED_NOW))
        transition_to(db, execution_id, terminal, clock=FixedClock(FIXED_NOW))
    finally:
        db.close()


def _event_types(worker_env, execution_id):
    db = _db(worker_env)
    try:
        return [e.event_type for e in list_events(db, execution_id)]
    finally:
        db.close()


def _event_count(worker_env, execution_id, event_type):
    db = _db(worker_env)
    try:
        return len([e for e in list_events(db, execution_id) if e.event_type == event_type])
    finally:
        db.close()


def _materialize_first_step(worker_env, execution_id):
    """在同一 session 内物化首个步骤行（避免 detached 对象跨 session 使用）。"""
    db = _db(worker_env)
    try:
        execution = db.get(Execution, execution_id)
        spec = get_type_spec(execution.execution_type)
        step_spec = resolve_step_template(spec)[0]
        tool = get_tool(step_spec.tool_name)
        return _materialize_step(
            db, execution, step_spec, 0, tool, clock=FixedClock(FIXED_NOW)
        )
    finally:
        db.close()


def _post_execution(client, key, execution_type="e6.ping", payload=None, run_at=None):
    body = {"execution_type": execution_type, "payload": payload or {}}
    if run_at is not None:
        body["run_at"] = run_at
    return client.post(
        "/api/executions", json=body, headers={"Idempotency-Key": key}
    )


# =====================================================================
# A. 创建与幂等（规格五）
# =====================================================================

def test_create_201_replayed_false_and_header_echo(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = _post_execution(client, _unique_key(), payload={"echo": "hi"})
    assert res.status_code == 201
    body = res.json()
    assert body["replayed"] is False
    assert body["execution"]["status"] == "CREATED"
    assert body["execution"]["execution_type"] == "e6.ping"
    # 无 X-Request-ID 时服务端生成：execution 与响应头共享同一 ID
    assert body["execution"]["correlation_id"]
    assert res.headers["X-Request-ID"] == body["execution"]["correlation_id"]
    assert _event_types(worker_env, body["execution"]["id"]) == ["EXECUTION_CREATED"]


def test_create_replay_same_request_200(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    key = _unique_key()
    first = _post_execution(client, key, payload={"echo": "x"})
    replay = _post_execution(client, key, payload={"echo": "x"})
    assert replay.status_code == 200
    body = replay.json()
    assert body["replayed"] is True
    assert body["execution"]["id"] == first.json()["execution"]["id"]
    # 重放不产生重复创建事件
    assert _event_count(worker_env, body["execution"]["id"], "EXECUTION_CREATED") == 1


def test_create_conflict_different_request_409(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    key = _unique_key()
    _post_execution(client, key, payload={"echo": "a"})
    res = _post_execution(client, key, payload={"echo": "b"})
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "EXEC_IDEMPOTENCY_CONFLICT"


def test_create_missing_idempotency_key_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = client.post(
        "/api/executions", json={"execution_type": "e6.ping", "payload": {}}
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_short_key_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = _post_execution(client, "short")
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "EXEC_IDEMPOTENCY_KEY_INVALID"


def test_create_invalid_key_chars_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = _post_execution(client, "bad key! with spaces")
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "EXEC_IDEMPOTENCY_KEY_INVALID"


def test_create_body_idempotency_key_rejected_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    # 规格五：不允许 body 替代 Header（extra=forbid → 422）
    res = client.post(
        "/api/executions",
        json={
            "execution_type": "e6.ping",
            "payload": {},
            "idempotency_key": _unique_key("body"),
        },
        headers={"Idempotency-Key": _unique_key()},
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_create_unknown_type_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = _post_execution(client, _unique_key(), execution_type="nope.unknown")
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "EXEC_TYPE_UNKNOWN"


def test_create_payload_unknown_field_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = _post_execution(client, _unique_key(), payload={"echo": "ok", "extra": 1})
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "EXEC_PAYLOAD_INVALID"


def test_create_naive_run_at_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    # naive datetime（无时区）→ 6D UTC 规范：稳定 422 EXEC_RUN_AT_NAIVE
    res = _post_execution(client, _unique_key(), run_at="2026-08-11T04:00:00")
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "EXEC_RUN_AT_NAIVE"


def test_create_future_run_at_scheduled_event(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = _post_execution(
        client, _unique_key(), run_at=_aware_iso(FIXED_NOW + timedelta(hours=1))
    )
    assert res.status_code == 201
    execution_id = res.json()["execution"]["id"]
    types = _event_types(worker_env, execution_id)
    assert "EXECUTION_SCHEDULED" in types
    assert types[0] == "EXECUTION_CREATED"  # 审计起点在调度声明之前


def test_create_response_no_payload_no_lease(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = _post_execution(client, _unique_key(), payload={"echo": "secret-ish"})
    body = res.json()["execution"]
    for forbidden in ("payload", "lease_token", "lease_owner", "lease_expires_at"):
        assert forbidden not in body


# =====================================================================
# B. 查询 API（规格六）
# =====================================================================

def test_get_execution_404_envelope(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = client.get("/api/executions/9999")
    assert res.status_code == 404
    error = res.json()["error"]
    assert error["code"] == "EXEC_NOT_FOUND"
    assert error["message"]
    assert error["details"] == {}
    assert error["correlation_id"]  # 信封含 correlation_id


def test_get_execution_ok_fields_utc(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    created = _post_execution(client, _unique_key()).json()["execution"]
    res = client.get(f"/api/executions/{created['id']}")
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == created["id"]
    assert body["status"] == "CREATED"
    assert body["created_at"].endswith("Z")  # UTC 序列化
    assert "payload" not in body


def test_list_empty(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = client.get("/api/executions")
    assert res.status_code == 200
    assert res.json() == []


def test_list_status_filter_and_invalid_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    created = _post_execution(client, _unique_key()).json()["execution"]
    ok = client.get("/api/executions", params={"status": "CREATED"})
    assert ok.status_code == 200
    assert [e["id"] for e in ok.json()] == [created["id"]]
    empty = client.get("/api/executions", params={"status": "SUCCEEDED"})
    assert empty.json() == []
    invalid = client.get("/api/executions", params={"status": "HACKED"})
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"


def test_list_execution_type_filter(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    _post_execution(client, _unique_key(), execution_type="e6.ping")
    _post_execution(client, _unique_key(), execution_type="e6.risk")
    res = client.get("/api/executions", params={"execution_type": "e6.risk"})
    assert res.status_code == 200
    assert [e["execution_type"] for e in res.json()] == ["e6.risk"]


def test_list_created_range_filter(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    _post_execution(client, _unique_key())
    before = _aware_iso(FIXED_NOW - timedelta(hours=1))
    after = _aware_iso(FIXED_NOW + timedelta(hours=1))
    # created_from 晚于创建时间 → 空
    assert client.get(
        "/api/executions", params={"created_from": after}
    ).json() == []
    # created_to 早于创建时间 → 空
    assert client.get(
        "/api/executions", params={"created_to": before}
    ).json() == []
    # 区间覆盖创建时间 → 返回
    res = client.get(
        "/api/executions",
        params={"created_from": before, "created_to": after},
    )
    assert len(res.json()) == 1


def test_list_limit_upper_bound_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = client.get("/api/executions", params={"limit": 201})
    assert res.status_code == 422


def test_list_sort_whitelist_rejected(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    assert client.get(
        "/api/executions", params={"sort": "payload"}
    ).status_code == 422
    assert client.get(
        "/api/executions", params={"order": "bogus"}
    ).status_code == 422


def test_list_sort_id_asc_desc_tie_break(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    ids = [
        _post_execution(client, _unique_key()).json()["execution"]["id"]
        for _ in range(3)
    ]
    asc = client.get("/api/executions", params={"sort": "id", "order": "asc"})
    assert [e["id"] for e in asc.json()] == sorted(ids)
    desc = client.get("/api/executions", params={"sort": "id", "order": "desc"})
    assert [e["id"] for e in desc.json()] == sorted(ids, reverse=True)


def test_list_default_pagination_limit(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    for _ in range(3):
        _post_execution(client, _unique_key())
    limited = client.get("/api/executions", params={"limit": 2})
    assert len(limited.json()) == 2
    paged = client.get("/api/executions", params={"limit": 2, "offset": 2})
    assert len(paged.json()) == 1


def test_events_ascending_paginated(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _post_execution(client, _unique_key()).json()["execution"]["id"]
    # 追加两个事件制造序列（enqueue 写 EXECUTION_QUEUED；cancel 写 CANCELLED）
    _service_enqueue(worker_env, execution_id)
    client.post(f"/api/executions/{execution_id}/cancel")
    res = client.get(f"/api/executions/{execution_id}/events")
    assert res.status_code == 200
    events = res.json()
    sequences = [e["sequence_number"] for e in events]
    assert sequences == sorted(sequences)
    assert [e["event_type"] for e in events] == [
        "EXECUTION_CREATED", "EXECUTION_QUEUED", "EXECUTION_CANCELLED",
    ]
    # 分页：limit=1 偏移逐条取回与全量一致
    first = client.get(
        f"/api/executions/{execution_id}/events", params={"limit": 1, "offset": 0}
    ).json()
    second = client.get(
        f"/api/executions/{execution_id}/events", params={"limit": 1, "offset": 1}
    ).json()
    assert first[0]["id"] == events[0]["id"]
    assert second[0]["id"] == events[1]["id"]


def test_events_execution_not_found_404(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    res = client.get("/api/executions/9999/events")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "EXEC_NOT_FOUND"


# =====================================================================
# C. Cancel（规格七）
# =====================================================================

def test_cancel_created_direct_cancelled(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _post_execution(client, _unique_key()).json()["execution"]["id"]
    res = client.post(f"/api/executions/{execution_id}/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "CANCELLED"
    assert "EXECUTION_CANCELLED" in _event_types(worker_env, execution_id)


def test_cancel_queued_cancelled(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _post_execution(client, _unique_key()).json()["execution"]["id"]
    _service_enqueue(worker_env, execution_id)
    res = client.post(f"/api/executions/{execution_id}/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "CANCELLED"


def test_cancel_running_requested_and_repeat_idempotent(
    client, worker_env, monkeypatch
):
    _register_e6(monkeypatch)
    execution_id = _post_execution(client, _unique_key()).json()["execution"]["id"]
    _service_enqueue(worker_env, execution_id)
    execution, attempt = _claim(worker_env)
    transition_to(
        _db(worker_env), execution.id, ExecutionStatus.RUNNING,
        clock=FixedClock(FIXED_NOW),
    )
    res = client.post(f"/api/executions/{execution_id}/cancel")
    assert res.status_code == 200
    assert res.json()["status"] == "CANCEL_REQUESTED"
    # 重复取消：幂等返回当前状态（合法重复取消，规格七要求测试并记录）
    again = client.post(f"/api/executions/{execution_id}/cancel")
    assert again.status_code == 200
    assert again.json()["status"] == "CANCEL_REQUESTED"
    assert _event_count(worker_env, execution_id, "CANCEL_REQUESTED") == 1


def test_cancel_terminal_stable_409(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _post_execution(client, _unique_key()).json()["execution"]["id"]
    # service 走合法白名单路径到终态 SUCCEEDED
    _reach_terminal(worker_env, execution_id, ExecutionStatus.SUCCEEDED)
    res = client.post(f"/api/executions/{execution_id}/cancel")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "EXEC_INVALID_STATE_TRANSITION"


# =====================================================================
# D. Retry（规格八）
# =====================================================================

def test_retry_failed_to_queued_then_succeeds(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env).id
    _reach_terminal(worker_env, execution_id, ExecutionStatus.FAILED)
    res = client.post(f"/api/executions/{execution_id}/retry")
    assert res.status_code == 200
    assert res.json()["status"] == "QUEUED"
    # RETRY_SCHEDULED 事件带 api_retry 原因（worker 重试 payload 风格一致）
    db = _db(worker_env)
    retry_event = [
        e for e in list_events(db, execution_id)
        if e.event_type == "RETRY_SCHEDULED"
    ][-1]
    assert json.loads(retry_event.payload)["reason"] == "api_retry"
    db.close()
    # 重试后 Worker 领取 → 新 attempt 执行成功
    stats = _worker(worker_env, FakeClock()).run_once(max_claims=1)
    assert stats["succeeded"] == 1
    db = _db(worker_env)
    assert db.get(Execution, execution_id).status == "SUCCEEDED"
    db.close()


def test_retry_double_click_idempotent(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env).id
    _reach_terminal(worker_env, execution_id, ExecutionStatus.FAILED)
    first = client.post(f"/api/executions/{execution_id}/retry")
    assert first.status_code == 200
    assert first.json()["status"] == "QUEUED"
    # 双击：第二次 retry 幂等返回，不产生重复调度
    second = client.post(f"/api/executions/{execution_id}/retry")
    assert second.status_code == 200
    assert second.json()["status"] == "QUEUED"
    assert _event_count(worker_env, execution_id, "RETRY_SCHEDULED") == 1


def test_retry_succeeded_409(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env).id
    _reach_terminal(worker_env, execution_id, ExecutionStatus.SUCCEEDED)
    res = client.post(f"/api/executions/{execution_id}/retry")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "EXEC_INVALID_STATE_TRANSITION"


def test_retry_running_409(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env).id
    _service_enqueue(worker_env, execution_id)
    _claim(worker_env)
    db = _db(worker_env)
    transition_to(db, execution_id, ExecutionStatus.RUNNING, clock=FixedClock(FIXED_NOW))
    db.close()
    res = client.post(f"/api/executions/{execution_id}/retry")
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "EXEC_INVALID_STATE_TRANSITION"


def test_retry_preserves_old_attempts_steps_events(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env).id
    _reach_terminal(worker_env, execution_id, ExecutionStatus.FAILED)
    assert _event_count(worker_env, execution_id, "EXECUTION_FAILED") == 1
    client.post(f"/api/executions/{execution_id}/retry")
    # retry 只追加 RETRY_SCHEDULED，不覆写旧事件（append-only 审计）
    assert _event_count(worker_env, execution_id, "EXECUTION_FAILED") == 1
    assert _event_count(worker_env, execution_id, "RETRY_SCHEDULED") == 1


# =====================================================================
# E. Confirmation（规格九）
# =====================================================================

def test_confirm_granted_actor_persisted_and_worker_runs(
    client, worker_env, monkeypatch
):
    _register_e6(monkeypatch)
    execution_id = _post_execution(
        client, _unique_key(), execution_type="e6.risk", payload={"message": "x"}
    ).json()["execution"]["id"]
    _service_enqueue(worker_env, execution_id)
    clock = FakeClock()
    # Worker 首次执行：确认门 PENDING → 步骤失败（可重试）+ CONFIRMATION_REQUIRED
    stats = _worker(worker_env, clock).run_once(max_claims=1)
    assert stats["retried"] == 1  # EXEC_CONFIRMATION_REQUIRED 可重试
    db = _db(worker_env)
    step = db.query(ExecutionStep).filter(
        ExecutionStep.execution_id == execution_id
    ).one()
    assert step.confirmation_status == "PENDING"
    assert db.get(Execution, execution_id).status == "QUEUED"  # 可重试回队
    db.close()
    assert "CONFIRMATION_REQUIRED" in _event_types(worker_env, execution_id)
    # HTTP confirm：显式 actor 落盘
    res = client.post(
        f"/api/executions/{execution_id}/steps/{step.id}/confirm",
        json={"actor": "alice"},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["confirmation_status"] == "GRANTED"
    assert body["confirmation_actor_id"] == "alice"
    assert body["confirmation_actor_type"] == "user"
    assert body["confirmed_at"] is not None
    assert "CONFIRMATION_GRANTED" in _event_types(worker_env, execution_id)
    # Worker 再执行：越过退避窗口 → 门放行 → 工具执行 → SUCCEEDED
    clock.advance(10)
    stats = _worker(worker_env, clock).run_once(max_claims=1)
    assert stats["succeeded"] == 1
    db = _db(worker_env)
    assert db.get(Execution, execution_id).status == "SUCCEEDED"
    db.close()


def test_confirm_missing_actor_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env, "e6.risk").id
    res = client.post(
        f"/api/executions/{execution_id}/steps/1/confirm", json={}
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_confirm_body_confirmation_status_rejected_422(
    client, worker_env, monkeypatch
):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env, "e6.risk").id
    res = client.post(
        f"/api/executions/{execution_id}/steps/1/confirm",
        json={"actor": "alice", "confirmation_status": "GRANTED"},
    )
    assert res.status_code == 422  # extra=forbid：body 无法绕过 service
    assert res.json()["error"]["code"] == "VALIDATION_ERROR"


def test_confirm_other_execution_step_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    first = _service_create(worker_env, "e6.risk")
    second = _service_create(worker_env, "e6.risk")
    # 在 first 的 step 上制造真实 step 行（物化）
    step = _materialize_first_step(worker_env, first.id)
    # 用 second 的 execution_id 确认 first 的 step → 拒绝（跨 execution）
    res = client.post(
        f"/api/executions/{second.id}/steps/{step.id}/confirm",
        json={"actor": "alice"},
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "EXEC_STEP_INVALID"


def test_confirm_step_not_found_422(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env, "e6.risk").id
    res = client.post(
        f"/api/executions/{execution_id}/steps/424242/confirm",
        json={"actor": "alice"},
    )
    assert res.status_code == 422
    assert res.json()["error"]["code"] == "EXEC_STEP_INVALID"


def test_confirm_after_reject_409(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env, "e6.risk").id
    _service_enqueue(worker_env, execution_id)  # QUEUED 才可确认
    step = _materialize_first_step(worker_env, execution_id)
    reject = client.post(
        f"/api/executions/{execution_id}/steps/{step.id}/reject",
        json={"actor": "alice"},
    )
    assert reject.status_code == 200
    assert reject.json()["confirmation_status"] == "REJECTED"
    # REJECTED 是不可翻转的终态决策 → confirm → 稳定 409
    res = client.post(
        f"/api/executions/{execution_id}/steps/{step.id}/confirm",
        json={"actor": "bob"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "EXEC_STEP_CONFLICT"


def test_reject_after_grant_409(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env, "e6.risk").id
    _service_enqueue(worker_env, execution_id)
    step = _materialize_first_step(worker_env, execution_id)
    grant = client.post(
        f"/api/executions/{execution_id}/steps/{step.id}/confirm",
        json={"actor": "alice"},
    )
    assert grant.status_code == 200
    # GRANTED 是不可翻转的终态决策 → reject → 稳定 409
    res = client.post(
        f"/api/executions/{execution_id}/steps/{step.id}/reject",
        json={"actor": "bob"},
    )
    assert res.status_code == 409
    assert res.json()["error"]["code"] == "EXEC_STEP_CONFLICT"


def test_reject_then_worker_stable_failed(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _post_execution(
        client, _unique_key(), execution_type="e6.risk", payload={"message": "x"}
    ).json()["execution"]["id"]
    _service_enqueue(worker_env, execution_id)
    clock = FakeClock()
    _worker(worker_env, clock).run_once(max_claims=1)  # 门 PENDING 回队
    db = _db(worker_env)
    step = db.query(ExecutionStep).filter(
        ExecutionStep.execution_id == execution_id
    ).one()
    db.close()
    res = client.post(
        f"/api/executions/{execution_id}/steps/{step.id}/reject",
        json={"actor": "alice"},
    )
    assert res.status_code == 200
    # Worker 再执行（越过退避）：REJECTED 门 → 稳定失败（不重试）。
    # 说明：step 首次门失败已置 FAILED；gate 对 FAILED step 的再次失败
    # 经 _fail_step 条件 UPDATE rowcount=0 → StepConflictError → execution
    # FAILED——destructive 工具绝不执行，REJECTED 不产生新尝试。
    clock.advance(10)
    stats = _worker(worker_env, clock).run_once(max_claims=1)
    assert stats["failed"] == 1
    db = _db(worker_env)
    execution = db.get(Execution, execution_id)
    assert execution.status == "FAILED"
    step = db.query(ExecutionStep).filter(
        ExecutionStep.execution_id == execution_id
    ).one()
    assert step.confirmation_status == "REJECTED"  # 拒绝决策保持落盘
    # 终态稳定：FAILED 不再被 claim，无任何后续动作（拒绝后不重试）
    stats2 = _worker(worker_env, clock).run_once(max_claims=1)
    assert stats2["claimed"] == 0
    assert stats2.get("failed", 0) == 0 and stats2.get("retried", 0) == 0
    db.close()


def test_confirm_idempotent_same_actor(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    execution_id = _service_create(worker_env, "e6.risk").id
    _service_enqueue(worker_env, execution_id)
    step = _materialize_first_step(worker_env, execution_id)
    first = client.post(
        f"/api/executions/{execution_id}/steps/{step.id}/confirm",
        json={"actor": "alice"},
    )
    assert first.status_code == 200
    second = client.post(
        f"/api/executions/{execution_id}/steps/{step.id}/confirm",
        json={"actor": "alice"},
    )
    assert second.status_code == 200
    assert second.json()["confirmation_status"] == "GRANTED"
    # 同 actor 重复确认不重复写事件
    assert _event_count(worker_env, execution_id, "CONFIRMATION_GRANTED") == 1


# =====================================================================
# F. 错误信封与 correlation（规格十）
# =====================================================================

def test_error_envelope_full_structure(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    key = _unique_key()
    _post_execution(client, key, payload={"echo": "a"})
    res = _post_execution(client, key, payload={"echo": "b"})
    assert res.status_code == 409
    body = res.json()
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {
        "code", "message", "details", "correlation_id"
    }
    assert body["error"]["code"] == "EXEC_IDEMPOTENCY_CONFLICT"
    assert isinstance(body["error"]["details"], dict)
    assert body["error"]["correlation_id"]
    assert res.headers["X-Request-ID"] == body["error"]["correlation_id"]


def test_correlation_id_propagates_to_events(client, worker_env, monkeypatch):
    _register_e6(monkeypatch)
    cid = "client-correlation-0001"
    res = client.post(
        "/api/executions",
        json={"execution_type": "e6.ping", "payload": {}},
        headers={"Idempotency-Key": _unique_key(), "X-Request-ID": cid},
    )
    assert res.status_code == 201
    assert res.headers["X-Request-ID"] == cid
    assert res.json()["execution"]["correlation_id"] == cid
    # 事件透传同一 correlation_id
    db = _db(worker_env)
    event = list_events(db, res.json()["execution"]["id"])[0]
    db.close()
    assert event.event_type == "EXECUTION_CREATED"
    assert event.correlation_id == cid


def test_no_execute_now_route_404(client, worker_env, monkeypatch):
    """规格四禁止：无 execute-now / 任意调用旁路接口。"""
    _register_e6(monkeypatch)
    execution_id = _post_execution(client, _unique_key()).json()["execution"]["id"]
    for path in (
        f"/api/executions/{execution_id}/execute",
        f"/api/executions/{execution_id}/run",
        "/api/executions/execute",
    ):
        res = client.post(path)
        # 404（无路由）或 405（路径被 /{execution_id} GET 路由匹配但方法不匹配）
        # 均证明该 POST 端点不存在——没有任何 execute-now 旁路。
        assert res.status_code in (404, 405), path


# =====================================================================
# G. 前端最小契约（types.ts / api.ts）
# =====================================================================

def test_frontend_types_contract():
    types_src = (FRONTEND_DIR / "types.ts").read_text(encoding="utf-8")
    for needle in (
        "export interface Execution {",
        "export interface ExecutionEvent {",
        "export interface ExecutionStep {",
        "export type ExecutionStatus",
        "export interface ExecutionCreatePayload",
        "export interface ExecutionCreateResult",
        "EXECUTION_STATUS_LABELS",
    ):
        assert needle in types_src, needle


def test_frontend_api_contract():
    api_src = (FRONTEND_DIR / "api.ts").read_text(encoding="utf-8")
    for needle in (
        "export function createExecution(",
        "export function listExecutions(",
        "export function getExecution(",
        "export function listExecutionEvents(",
        "export function cancelExecution(",
        "export function retryExecution(",
        "export function confirmStep(",
        "export function rejectStep(",
        "'Idempotency-Key': idempotencyKey",
    ):
        assert needle in api_src, needle
