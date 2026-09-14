"""Phase 7.2A：Execution UI 所需的最小后端公开契约。

只验证两个新增 HTTP 能力：显式 enqueue 与只读 steps。测试使用
conftest 的隔离 SQLite 数据库和固定时钟，不触碰开发 jarvis.db。
"""

import itertools
import json
from datetime import datetime, timezone

from app.core.clock import FixedClock
from app.models.execution import Execution
from app.models.execution_step import ExecutionStep, StepStatus
from app.services.execution_events import list_events
from app.services.execution_registry import (
    EXECUTION_TOOLS,
    EXECUTION_TYPES,
    ExecutionTypeSpec,
    StepSpec,
    ToolSpec,
    get_tool,
    get_type_spec,
    resolve_step_template,
)
from app.services.execution_steps import _materialize_step
from app.services.execution_worker import ExecutionWorker


FIXED_NOW = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)


_key_counter = itertools.count(1)


def _key(prefix: str = "phase72a") -> str:
    return f"{prefix}-{next(_key_counter):08d}"


def _create(client, execution_type="system.ping", payload=None):
    return client.post(
        "/api/executions",
        json={"execution_type": execution_type, "payload": payload or {}},
        headers={"Idempotency-Key": _key()},
    )


def _db(worker_env):
    _, session_factory = worker_env
    return session_factory()


def _materialize(worker_env, execution_id: int, *, reverse: bool = False):
    db = _db(worker_env)
    try:
        execution = db.get(Execution, execution_id)
        spec = get_type_spec(execution.execution_type)
        template = list(enumerate(resolve_step_template(spec)))
        if reverse:
            template.reverse()
        for index, step_spec in template:
            _materialize_step(
                db,
                execution,
                step_spec,
                index,
                get_tool(step_spec.tool_name),
                clock=FixedClock(FIXED_NOW),
            )
    finally:
        db.close()


def test_http_enqueue_created_to_queued_without_executing_tool(client, worker_env):
    created = _create(client)
    assert created.status_code == 201
    execution_id = created.json()["execution"]["id"]

    response = client.post(f"/api/executions/{execution_id}/enqueue")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["queued_at"] is not None
    for forbidden in ("payload", "lease_token", "lease_owner", "lease_expires_at"):
        assert forbidden not in body

    db = _db(worker_env)
    try:
        assert db.query(ExecutionStep).filter_by(execution_id=execution_id).count() == 0
        assert [event.event_type for event in list_events(db, execution_id)] == [
            "EXECUTION_CREATED",
            "EXECUTION_QUEUED",
        ]
    finally:
        db.close()


def test_http_enqueue_missing_execution_safe_error_and_request_id(client):
    request_id = "phase72a-missing-request"
    response = client.post(
        "/api/executions/999999/enqueue",
        headers={"X-Request-ID": request_id},
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == request_id
    assert response.json()["error"] == {
        "code": "EXEC_NOT_FOUND",
        "message": "Execution 999999 does not exist",
        "details": {},
        "correlation_id": request_id,
    }


def test_http_enqueue_illegal_state_fails_closed_and_does_not_duplicate_event(
    client, worker_env
):
    execution_id = _create(client).json()["execution"]["id"]
    assert client.post(f"/api/executions/{execution_id}/enqueue").status_code == 200

    request_id = "phase72a-repeat-enqueue"
    response = client.post(
        f"/api/executions/{execution_id}/enqueue",
        headers={"X-Request-ID": request_id},
    )

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "EXEC_INVALID_STATE_TRANSITION"
    assert error["correlation_id"] == request_id
    assert response.headers["X-Request-ID"] == request_id
    current = client.get(f"/api/executions/{execution_id}").json()
    assert current["status"] == "QUEUED"

    db = _db(worker_env)
    try:
        queued_events = [
            event for event in list_events(db, execution_id)
            if event.event_type == "EXECUTION_QUEUED"
        ]
        assert len(queued_events) == 1
    finally:
        db.close()


def test_http_enqueue_does_not_bypass_confirmation_gate(client, worker_env):
    execution_id = _create(
        client,
        execution_type="documents.reindex",
        payload={"document_id": 999999},
    ).json()["execution"]["id"]

    response = client.post(f"/api/executions/{execution_id}/enqueue")
    assert response.status_code == 200
    assert response.json()["status"] == "QUEUED"

    db = _db(worker_env)
    try:
        assert db.query(ExecutionStep).filter_by(execution_id=execution_id).count() == 0
        event_types = [event.event_type for event in list_events(db, execution_id)]
        assert "CONFIRMATION_GRANTED" not in event_types
        assert "STEP_STARTED" not in event_types
    finally:
        db.close()


def test_get_steps_empty_and_missing_execution_contract(client):
    execution_id = _create(client).json()["execution"]["id"]
    response = client.get(f"/api/executions/{execution_id}/steps")
    assert response.status_code == 200
    assert response.json() == []

    request_id = "phase72a-missing-steps"
    missing = client.get(
        "/api/executions/999999/steps",
        headers={"X-Request-ID": request_id},
    )
    assert missing.status_code == 404
    assert missing.headers["X-Request-ID"] == request_id
    assert missing.json()["error"]["code"] == "EXEC_NOT_FOUND"
    assert missing.json()["error"]["correlation_id"] == request_id


def test_get_steps_returns_current_safe_fields_in_stable_order(
    client, worker_env, monkeypatch
):
    monkeypatch.setitem(
        EXECUTION_TYPES,
        "phase72a.multi",
        ExecutionTypeSpec(
            name="phase72a.multi",
            payload_schema_version=1,
            payload_allowed_fields=frozenset(),
            max_attempts=3,
            timeout_seconds=None,
            steps=(
                StepSpec(step_key="first", tool_name="phase72a.first"),
                StepSpec(step_key="second", tool_name="phase72a.second"),
            ),
        ),
    )
    for name in ("phase72a.first", "phase72a.second"):
        monkeypatch.setitem(
            EXECUTION_TOOLS,
            name,
            ToolSpec(
                name=name,
                handler=lambda _value: {"ok": True},
                destructive=False,
                requires_confirmation=False,
                replay_safe=True,
            ),
        )

    execution_id = _create(client, execution_type="phase72a.multi").json()[
        "execution"
    ]["id"]
    _materialize(worker_env, execution_id, reverse=True)

    db = _db(worker_env)
    try:
        first = db.query(ExecutionStep).filter_by(
            execution_id=execution_id, step_key="first"
        ).one()
        first.status = StepStatus.RUNNING.value
        first.error_code = "SAFE_TEST_ERROR"
        first.error_message = "safe public message"
        first.output_json = json.dumps({"token": "must-not-leak"})
        db.commit()
    finally:
        db.close()

    response = client.get(f"/api/executions/{execution_id}/steps")
    assert response.status_code == 200
    steps = response.json()
    assert [step["step_key"] for step in steps] == ["first", "second"]
    assert [step["step_index"] for step in steps] == [0, 1]
    assert steps[0]["status"] == "RUNNING"
    assert steps[0]["error_code"] == "SAFE_TEST_ERROR"
    assert steps[0]["error_message"] == "safe public message"
    assert steps[0]["updated_at"] is not None
    forbidden = {
        "attempt_id",
        "input_json",
        "input_hash",
        "output_json",
        "lease_token",
        "lease_owner",
        "lease_expires_at",
        "traceback",
    }
    assert forbidden.isdisjoint(steps[0])


def test_refresh_steps_exposes_public_id_for_existing_confirm_reject_apis(
    client, worker_env
):
    execution_id = _create(
        client,
        execution_type="documents.reindex",
        payload={"document_id": 999999},
    ).json()["execution"]["id"]
    assert client.post(f"/api/executions/{execution_id}/enqueue").status_code == 200

    _, session_factory = worker_env
    worker = ExecutionWorker(
        session_factory,
        worker_id="phase72a-worker",
        clock=FixedClock(FIXED_NOW),
        lease_ttl_seconds=60,
    )
    stats = worker.run_once(max_claims=1)
    assert stats["retried"] == 1

    refreshed = client.get(f"/api/executions/{execution_id}/steps")
    assert refreshed.status_code == 200
    steps = refreshed.json()
    assert len(steps) == 1
    step = steps[0]
    assert step["tool_name"] == "documents.reindex"
    assert step["requires_confirmation"] is True
    assert step["confirmation_status"] == "PENDING"
    assert isinstance(step["id"], int)

    rejected = client.post(
        f"/api/executions/{execution_id}/steps/{step['id']}/reject",
        json={"actor": "phase72a-user"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["confirmation_status"] == "REJECTED"
