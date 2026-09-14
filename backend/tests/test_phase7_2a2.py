"""Phase 7.2A2：Execution Detail 安全业务 Target 契约。"""

import itertools
import json

import pytest

from app.core.clock import FixedClock
from app.models.execution import Execution
from app.models.execution_step import ExecutionStep
from app.services.execution_events import list_events
from app.services.execution_worker import ExecutionWorker
from tests.conftest import FIXED_NOW


_key_counter = itertools.count(1)


def _create(client, execution_type: str, payload: dict):
    return client.post(
        "/api/executions",
        json={"execution_type": execution_type, "payload": payload},
        headers={"Idempotency-Key": f"phase72a2-{next(_key_counter):08d}"},
    )


def _db(worker_env):
    _, session_factory = worker_env
    return session_factory()


def test_documents_reindex_detail_exposes_only_typed_document_target(client):
    created = _create(client, "documents.reindex", {"document_id": 4321})
    assert created.status_code == 201
    execution_id = created.json()["execution"]["id"]

    response = client.get(f"/api/executions/{execution_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["target"] == {"type": "document", "document_id": 4321}
    forbidden = {
        "payload",
        "input_json",
        "output_json",
        "lease_token",
        "lease_owner",
        "lease_expires_at",
        "traceback",
        "secret",
        "worker_id",
    }
    assert forbidden.isdisjoint(body)


def test_execution_list_stays_lightweight_without_target(client):
    created = _create(client, "documents.reindex", {"document_id": 73})
    execution_id = created.json()["execution"]["id"]

    response = client.get("/api/executions")

    assert response.status_code == 200
    item = next(row for row in response.json() if row["id"] == execution_id)
    assert "target" not in item
    assert "payload" not in item


def test_other_execution_types_never_project_document_target_or_message(client):
    secret_message = "message-must-not-be-projected"
    created = _create(
        client,
        "reminder.send",
        {"reminder_id": 11, "task_id": 22, "message": secret_message},
    )
    execution_id = created.json()["execution"]["id"]

    body = client.get(f"/api/executions/{execution_id}").json()

    assert body["target"] is None
    assert secret_message not in json.dumps(body)
    assert "payload" not in body


@pytest.mark.parametrize(
    "stored_payload",
    ["not-json", "{}", '{"document_id":"4321"}', '{"document_id":true}', "[]"],
)
def test_malformed_documents_reindex_payload_fails_closed_as_null_target(
    client, worker_env, stored_payload
):
    created = _create(client, "documents.reindex", {"document_id": 4321})
    execution_id = created.json()["execution"]["id"]
    db = _db(worker_env)
    try:
        execution = db.get(Execution, execution_id)
        execution.payload = stored_payload
        db.commit()
    finally:
        db.close()

    response = client.get(f"/api/executions/{execution_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["target"] is None
    assert stored_payload not in json.dumps(body)
    assert "traceback" not in body


def test_detail_get_is_pure_read_and_preserves_confirmation_state(
    client, worker_env
):
    created = _create(client, "documents.reindex", {"document_id": 999999})
    execution_id = created.json()["execution"]["id"]
    assert client.post(f"/api/executions/{execution_id}/enqueue").status_code == 200

    _, session_factory = worker_env
    worker = ExecutionWorker(
        session_factory,
        worker_id="phase72a2-worker",
        clock=FixedClock(FIXED_NOW),
        lease_ttl_seconds=60,
    )
    assert worker.run_once(max_claims=1)["retried"] == 1

    db = _db(worker_env)
    try:
        execution_before = db.get(Execution, execution_id)
        step_before = db.query(ExecutionStep).filter_by(
            execution_id=execution_id
        ).one()
        snapshot = (
            execution_before.status,
            step_before.status,
            step_before.confirmation_status,
            db.query(ExecutionStep).filter_by(execution_id=execution_id).count(),
            len(list_events(db, execution_id)),
        )
    finally:
        db.close()

    detail = client.get(f"/api/executions/{execution_id}")
    steps = client.get(f"/api/executions/{execution_id}/steps")

    assert detail.status_code == 200
    assert steps.status_code == 200
    assert detail.json()["target"] == {"type": "document", "document_id": 999999}
    assert len(steps.json()) == 1
    assert isinstance(steps.json()[0]["id"], int)
    assert steps.json()[0]["confirmation_status"] == "PENDING"
    for body in (detail.json(), steps.json()[0]):
        for forbidden in (
            "payload",
            "input_json",
            "lease_token",
            "lease_owner",
            "lease_expires_at",
        ):
            assert forbidden not in body

    db = _db(worker_env)
    try:
        execution_after = db.get(Execution, execution_id)
        step_after = db.query(ExecutionStep).filter_by(
            execution_id=execution_id
        ).one()
        assert (
            execution_after.status,
            step_after.status,
            step_after.confirmation_status,
            db.query(ExecutionStep).filter_by(execution_id=execution_id).count(),
            len(list_events(db, execution_id)),
        ) == snapshot
    finally:
        db.close()
