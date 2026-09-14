"""Phase 8B explicit Action Proposal confirmation and isolation tests."""

import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from app.models.execution import Execution
from app.models.execution_attempt import AttemptStatus, ExecutionAttempt
from app.models.execution_event import ExecutionEvent
from app.models.execution_step import ExecutionStep
from app.core.clock import FixedClock, get_clock
from app.main import app
from app.services.execution_claim import check_timeouts, claim_next, recover_expired


API = "/api/conversations"


def make_conversation(client) -> int:
    response = client.post(API, json={"title": "Phase 8B"})
    assert response.status_code == 201
    return response.json()["id"]


def task_proposal(*, title="完成报告", due_at=None):
    return {
        "proposal": {
            "action": "create_task",
            "task": {"title": title, "due_at": due_at},
            "reminder": None,
        }
    }


def reminder_proposal(
    *,
    title="交报告",
    due_at=None,
    remind_at="2026-08-12T07:00:00Z",
):
    return {
        "proposal": {
            "action": "create_task_with_reminder",
            "task": {"title": title, "due_at": due_at},
            "reminder": {"remind_at": remind_at},
        }
    }


def confirm(client, conversation_id, body, key="phase8b-key-001"):
    return client.post(
        f"{API}/{conversation_id}/action-proposals/confirm",
        json=body,
        headers={"Idempotency-Key": key},
    )


def counts(client):
    return {
        "tasks": len(client.get("/api/tasks").json()),
        "reminders": len(client.get("/api/reminders").json()),
        "executions": len(client.get("/api/executions").json()),
    }


def receipt_count(worker_env):
    _, session_factory = worker_env
    db = session_factory()
    try:
        return db.query(Execution).count()
    finally:
        db.close()


def test_create_task_success_returns_authoritative_record(client, worker_env):
    cid = make_conversation(client)
    response = confirm(client, cid, task_proposal())
    assert response.status_code == 201
    result = response.json()
    assert result["action"] == "create_task"
    assert result["replayed"] is False
    assert result["task"]["id"] > 0
    assert result["task"]["title"] == "完成报告"
    assert result["task"]["status"] == "DRAFT"
    assert result["task"]["due_at"] is None
    assert result["reminder"] is None
    assert client.get(f"/api/tasks/{result['task']['id']}").json() == result["task"]
    assert counts(client) == {"tasks": 1, "reminders": 0, "executions": 0}
    assert receipt_count(worker_env) == 1


def test_create_task_with_reminder_is_linked_and_due_remains_null(client, worker_env):
    cid = make_conversation(client)
    response = confirm(client, cid, reminder_proposal())
    assert response.status_code == 201
    result = response.json()
    assert result["action"] == "create_task_with_reminder"
    assert result["task"]["due_at"] is None
    assert result["reminder"]["task_id"] == result["task"]["id"]
    assert result["reminder"]["remind_at"] == "2026-08-12T07:00:00Z"
    assert counts(client) == {"tasks": 1, "reminders": 1, "executions": 0}
    assert receipt_count(worker_env) == 1


def test_explicit_due_and_reminder_remain_distinct(client):
    cid = make_conversation(client)
    response = confirm(
        client,
        cid,
        reminder_proposal(
            due_at="2026-08-14T09:00:00Z",
            remind_at="2026-08-12T07:00:00Z",
        ),
    )
    result = response.json()
    assert response.status_code == 201
    assert result["task"]["due_at"] == "2026-08-14T09:00:00Z"
    assert result["reminder"]["remind_at"] == "2026-08-12T07:00:00Z"


def test_double_confirm_replays_one_logical_result(client, worker_env):
    cid = make_conversation(client)
    first = confirm(client, cid, reminder_proposal(), key="double-confirm-001")
    second = confirm(client, cid, reminder_proposal(), key="double-confirm-001")
    assert first.status_code == 201
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["confirmation_id"] == first.json()["confirmation_id"]
    assert second.json()["task"]["id"] == first.json()["task"]["id"]
    assert second.json()["reminder"]["id"] == first.json()["reminder"]["id"]
    assert counts(client) == {"tasks": 1, "reminders": 1, "executions": 0}
    assert receipt_count(worker_env) == 1


def test_response_loss_retry_recovers_committed_result(client, worker_env):
    cid = make_conversation(client)
    lost_response = confirm(client, cid, task_proposal(), key="response-loss-001")
    assert lost_response.status_code == 201  # simulate client losing this response

    recovered = confirm(client, cid, task_proposal(), key="response-loss-001")
    assert recovered.status_code == 200
    assert recovered.json()["replayed"] is True
    assert recovered.json()["task"]["id"] == lost_response.json()["task"]["id"]
    assert counts(client) == {"tasks": 1, "reminders": 0, "executions": 0}
    assert receipt_count(worker_env) == 1


def test_response_loss_replay_still_works_after_reminder_time_passes(client, worker_env):
    cid = make_conversation(client)
    body = reminder_proposal()
    first = confirm(client, cid, body, key="late-replay-key-001")
    assert first.status_code == 201

    app.dependency_overrides[get_clock] = lambda: FixedClock(
        datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)
    )
    recovered = confirm(client, cid, body, key="late-replay-key-001")
    assert recovered.status_code == 200
    assert recovered.json()["replayed"] is True
    assert recovered.json()["task"]["id"] == first.json()["task"]["id"]
    assert recovered.json()["reminder"]["id"] == first.json()["reminder"]["id"]
    assert counts(client) == {"tasks": 1, "reminders": 1, "executions": 0}
    assert receipt_count(worker_env) == 1


def test_concurrent_double_confirm_is_database_idempotent(client, worker_env):
    cid = make_conversation(client)
    body = reminder_proposal()

    def submit():
        return confirm(client, cid, body, key="concurrent-confirm-001")

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: submit(), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 201]
    results = [response.json() for response in responses]
    assert len({result["confirmation_id"] for result in results}) == 1
    assert len({result["task"]["id"] for result in results}) == 1
    assert len({result["reminder"]["id"] for result in results}) == 1
    assert counts(client) == {"tasks": 1, "reminders": 1, "executions": 0}
    assert receipt_count(worker_env) == 1


def test_same_key_different_request_conflicts_without_duplicate(client, worker_env):
    cid = make_conversation(client)
    first = confirm(client, cid, task_proposal(title="报告 A"), key="conflict-key-001")
    conflict = confirm(client, cid, task_proposal(title="报告 B"), key="conflict-key-001")
    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "EXEC_IDEMPOTENCY_CONFLICT"
    assert conflict.json()["error"]["correlation_id"]
    assert counts(client) == {"tasks": 1, "reminders": 0, "executions": 0}
    assert receipt_count(worker_env) == 1


def test_reminder_failure_rolls_back_task_receipt_and_reminder(client, worker_env, monkeypatch):
    cid = make_conversation(client)

    def fail_reminder(*args, **kwargs):
        raise RuntimeError("private database detail must not leak")

    monkeypatch.setattr(
        "app.services.action_confirm_service.reminder_service.create_for_action_confirmation",
        fail_reminder,
    )
    response = confirm(client, cid, reminder_proposal(), key="rollback-key-001")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert "private database" not in str(body)
    assert counts(client) == {"tasks": 0, "reminders": 0, "executions": 0}
    assert receipt_count(worker_env) == 0


@pytest.mark.parametrize(
    "action",
    ["documents.reindex", "system.ping", "delete_task", "send_email"],
)
def test_tampered_action_fails_closed(client, action):
    cid = make_conversation(client)
    body = task_proposal()
    body["proposal"]["action"] = action
    response = confirm(client, cid, body, key=f"tamper-{action.replace('.', '-')}-001")
    assert response.status_code == 422
    assert counts(client) == {"tasks": 0, "reminders": 0, "executions": 0}


@pytest.mark.parametrize("field", ["tool_name", "execution_type", "handler", "lease_token"])
def test_dynamic_dispatch_fields_are_forbidden(client, field):
    cid = make_conversation(client)
    body = task_proposal()
    body["proposal"][field] = "system.ping"
    response = confirm(client, cid, body, key=f"extra-{field}-001")
    assert response.status_code == 422
    assert counts(client) == {"tasks": 0, "reminders": 0, "executions": 0}


@pytest.mark.parametrize(
    "body",
    [
        {
            "proposal": {
                "status": "UNSUPPORTED",
                "action": None,
                "task": None,
                "reminder": None,
            }
        },
        {
            "proposal": {
                "status": "INVALID",
                "action": None,
                "task": None,
                "reminder": None,
            }
        },
        {
            "proposal": {
                "status": "NEEDS_CLARIFICATION",
                "action": "create_task_with_reminder",
                "task": {"title": "交报告", "due_at": None},
                "reminder": {"remind_at": None},
            }
        },
    ],
)
def test_non_ready_proposals_cannot_be_confirmed(client, body):
    cid = make_conversation(client)
    response = confirm(client, cid, body, key="non-ready-key-001")
    assert response.status_code == 422
    assert counts(client) == {"tasks": 0, "reminders": 0, "executions": 0}


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (reminder_proposal(remind_at="2026-08-10T07:00:00Z"), "ACTION_CONFIRM_TIME_NOT_FUTURE"),
        (task_proposal(due_at="2026-08-10T07:00:00Z"), "ACTION_CONFIRM_TIME_NOT_FUTURE"),
    ],
)
def test_past_times_are_revalidated_at_confirm(client, body, expected_code):
    cid = make_conversation(client)
    response = confirm(client, cid, body, key="past-time-key-001")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == expected_code
    assert counts(client) == {"tasks": 0, "reminders": 0, "executions": 0}


@pytest.mark.parametrize(
    "body",
    [
        task_proposal(title="   "),
        task_proposal(due_at="2026-08-12T15:00:00"),
        reminder_proposal(remind_at="not-a-datetime"),
        reminder_proposal(remind_at="2026-08-12T15:00:00"),
    ],
)
def test_malformed_confirm_fields_rejected_before_writes(client, body):
    cid = make_conversation(client)
    response = confirm(client, cid, body, key="malformed-key-001")
    assert response.status_code == 422
    assert counts(client) == {"tasks": 0, "reminders": 0, "executions": 0}


def test_idempotency_header_is_required_and_strict(client):
    cid = make_conversation(client)
    missing = client.post(
        f"{API}/{cid}/action-proposals/confirm",
        json=task_proposal(),
    )
    invalid = confirm(client, cid, task_proposal(), key="short")
    assert missing.status_code == 422
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "EXEC_IDEMPOTENCY_KEY_INVALID"
    assert counts(client) == {"tasks": 0, "reminders": 0, "executions": 0}


def test_receipt_is_terminal_audited_and_has_no_steps_or_attempts(client, worker_env):
    cid = make_conversation(client)
    result = confirm(client, cid, task_proposal(), key="receipt-key-001").json()
    receipt_id = result["confirmation_id"]
    _, session_factory = worker_env
    db = session_factory()
    try:
        receipt = db.get(Execution, receipt_id)
        events = (
            db.query(ExecutionEvent)
            .filter(ExecutionEvent.execution_id == receipt_id)
            .order_by(ExecutionEvent.sequence_number)
            .all()
        )
        assert receipt.execution_type == "action.confirm"
        assert receipt.status == "SUCCEEDED"
        assert receipt.owner_id == f"conversation:{cid}"
        assert receipt.attempt_count == 0
        assert db.query(ExecutionAttempt).filter_by(execution_id=receipt_id).count() == 0
        assert db.query(ExecutionStep).filter_by(execution_id=receipt_id).count() == 0
        assert [event.event_type for event in events] == [
            "EXECUTION_CREATED",
            "EXECUTION_SUCCEEDED",
        ]
        assert json.loads(events[-1].payload) == {
            "task_id": result["task"]["id"],
            "reminder_id": None,
        }
    finally:
        db.close()


def test_internal_receipt_is_hidden_from_all_public_execution_reads(client, worker_env):
    cid = make_conversation(client)
    result = confirm(client, cid, task_proposal(), key="hidden-receipt-001").json()
    receipt_id = result["confirmation_id"]

    # Prove that public-type membership, not the normal conversation owner,
    # is the decisive isolation boundary.
    _, session_factory = worker_env
    db = session_factory()
    try:
        receipt = db.get(Execution, receipt_id)
        receipt.owner_id = "default"
        db.commit()
    finally:
        db.close()

    assert client.get("/api/executions").json() == []
    assert client.get(
        "/api/executions", params={"execution_type": "action.confirm"}
    ).json() == []
    for suffix in ("", "/events", "/steps"):
        response = client.get(f"/api/executions/{receipt_id}{suffix}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "EXEC_NOT_FOUND"


def test_internal_receipt_rejects_all_public_execution_operations(client):
    cid = make_conversation(client)
    receipt_id = confirm(
        client, cid, task_proposal(), key="operation-isolation-001"
    ).json()["confirmation_id"]

    for operation in ("enqueue", "cancel", "retry"):
        response = client.post(f"/api/executions/{receipt_id}/{operation}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "EXEC_NOT_FOUND"
    for operation in ("confirm", "reject"):
        response = client.post(
            f"/api/executions/{receipt_id}/steps/1/{operation}",
            json={"actor": "audit-user"},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "EXEC_NOT_FOUND"


def test_worker_claim_recovery_and_timeout_ignore_internal_receipt(client, worker_env):
    cid = make_conversation(client)
    receipt_id = confirm(
        client, cid, task_proposal(), key="worker-isolation-001"
    ).json()["confirmation_id"]
    _, session_factory = worker_env
    db = session_factory()
    now = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
    db_now = now.replace(tzinfo=None)
    try:
        receipt = db.get(Execution, receipt_id)
        receipt.status = "QUEUED"  # adversarial state: type boundary must still win
        receipt.finished_at = None
        db.commit()

        assert claim_next(db, "audit-worker", clock=FixedClock(now)) is None
        assert db.query(ExecutionAttempt).filter_by(execution_id=receipt_id).count() == 0
        assert db.query(ExecutionStep).filter_by(execution_id=receipt_id).count() == 0

        receipt.status = "CLAIMED"
        receipt.attempt_count = 1
        receipt.lease_owner = "audit-worker"
        receipt.lease_expires_at = db_now - timedelta(minutes=1)
        attempt = ExecutionAttempt(
            execution_id=receipt_id,
            attempt_number=1,
            worker_id="audit-worker",
            lease_token="internal-receipt-must-not-recover",
            lease_expires_at=db_now - timedelta(minutes=1),
            status=AttemptStatus.CLAIMED.value,
            created_at=db_now - timedelta(minutes=2),
            updated_at=db_now - timedelta(minutes=2),
        )
        db.add(attempt)
        db.commit()

        assert recover_expired(db, clock=FixedClock(now)) == 0
        db.refresh(receipt)
        db.refresh(attempt)
        assert receipt.status == "CLAIMED"
        assert attempt.status == AttemptStatus.CLAIMED.value

        receipt.status = "RUNNING"
        receipt.started_at = db_now - timedelta(minutes=1)
        receipt.timeout_seconds = 1
        attempt.status = AttemptStatus.RUNNING.value
        db.commit()
        assert check_timeouts(db, clock=FixedClock(now)) == 0
        db.refresh(receipt)
        assert receipt.status == "RUNNING"
        assert db.query(ExecutionStep).filter_by(execution_id=receipt_id).count() == 0
    finally:
        db.close()


def test_same_idempotency_key_is_scoped_by_conversation(client, worker_env):
    first_cid = make_conversation(client)
    second_cid = make_conversation(client)
    first = confirm(client, first_cid, task_proposal(), key="cross-conversation-001")
    second = confirm(client, second_cid, task_proposal(), key="cross-conversation-001")

    assert first.status_code == second.status_code == 201
    assert first.json()["confirmation_id"] != second.json()["confirmation_id"]
    assert first.json()["task"]["id"] != second.json()["task"]["id"]
    assert counts(client) == {"tasks": 2, "reminders": 0, "executions": 0}
    assert receipt_count(worker_env) == 2


def test_execution_ui_source_uses_the_isolated_public_list_api():
    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    panel_source = (
        frontend / "components" / "executions" / "ExecutionPanel.tsx"
    ).read_text(encoding="utf-8")
    api_source = (frontend / "api.ts").read_text(encoding="utf-8")
    assert "listExecutions({" in panel_source
    assert "request<Execution[]>(`/api/executions${suffix}`)" in api_source


def test_internal_receipt_type_cannot_be_created_via_execution_api(client):
    response = client.post(
        "/api/executions",
        json={"execution_type": "action.confirm", "payload": {}},
        headers={"Idempotency-Key": "public-forge-001"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "EXEC_TYPE_UNKNOWN"


def test_normal_chat_regression(client, fake_provider):
    cid = make_conversation(client)
    fake_provider.reply = "普通聊天仍然正常。"
    response = client.post(f"{API}/{cid}/messages", json={"content": "普通问题"})
    assert response.status_code == 201
    assert response.json()["assistant_message"]["content"] == fake_provider.reply
    assert counts(client) == {"tasks": 0, "reminders": 0, "executions": 0}


def test_frontend_confirm_contract_is_explicit_guarded_and_typed():
    frontend = Path(__file__).resolve().parents[2] / "frontend" / "src"
    types_source = (frontend / "types.ts").read_text(encoding="utf-8")
    api_source = (frontend / "api.ts").read_text(encoding="utf-8")
    chat_source = (frontend / "components" / "chat" / "ChatView.tsx").read_text(
        encoding="utf-8"
    )
    panel_source = (
        frontend / "components" / "chat" / "ActionProposalConfirmPanel.tsx"
    ).read_text(encoding="utf-8")
    preview_source = (
        frontend / "components" / "chat" / "ActionProposalCard.tsx"
    ).read_text(encoding="utf-8")

    assert "export type ConfirmableActionProposal" in types_source
    assert "ActionProposalConfirmation" in types_source
    assert "Idempotency-Key" in api_source
    assert "/action-proposals/confirm" in api_source
    assert "window.crypto.randomUUID()" in chat_source
    assert "actionConfirmGuardRef" in chat_source
    assert "setActionProposals([" in chat_source
    assert "setActionProposals([])" in chat_source
    assert "proposal.status === 'READY'" in panel_source
    assert 'type="button"' in panel_source
    assert "disabled={confirming}" in panel_source
    assert "Task #{result.task.id}" in panel_source
    assert "Reminder #{result.reminder.id}" in panel_source
    assert "<button" not in preview_source
