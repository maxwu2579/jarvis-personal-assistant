"""Phase 10B explicit Memory Proposal & Confirmation: API, policy, transactions,
idempotency, concurrency, expiry, privacy and isolation. Fake provider only."""

import json
import logging
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.clock import FixedClock
from app.core.database import _set_sqlite_pragma
from app.llm.base import LLMNotConfiguredError, LLMTimeoutError, LLMUpstreamError
from app.llm.factory import get_llm_provider
from app.llm.memory_prompts import MEMORY_CANDIDATE_SYSTEM_PROMPT
from app.main import app
from app.models.memory_receipt import MemoryConfirmationReceipt
from app.services import memory_proposal_service as service
from app.services.memory_proposal_store import (
    MemoryProposalStore,
    PendingMemoryProposal,
    get_memory_proposal_store,
)
from app.services.memory_service import MemoryError

API = '/api/memories'
PROPOSALS = '/api/memories/proposals'
FIXED = datetime(2026, 8, 11, 4, 0, tzinfo=timezone.utc)
STUDY_EVENING = 'Remember that I usually study in the evening.'
CHANGE_MORNING = 'Change my preferred study time to the morning.'
DURATION_45 = '记住我喜欢每次学习45分钟'
DAYS_MW = '记住我通常周一和周三有空'
OTHER_TABLES = ['tasks', 'reminders', 'executions', 'execution_attempts', 'execution_steps', 'execution_events',
                'documents', 'document_chunks', 'notifications', 'messages', 'conversations', 'rag_answers',
                'task_proposals']


class Mono:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture()
def mono():
    return Mono()


@pytest.fixture()
def store(client, mono):
    s = MemoryProposalStore(monotonic=mono)
    app.dependency_overrides[get_memory_proposal_store] = lambda: s
    return s


@pytest.fixture()
def engine(client, tmp_path):
    e = create_engine(f'sqlite:///{tmp_path / "test_tasks.db"}', connect_args={'check_same_thread': False})
    event.listen(e, 'connect', _set_sqlite_pragma)
    yield e
    e.dispose()


def cand(intent, key='none', *, study='none', minutes=0, days=None, evidence):
    return {'intent': intent, 'canonical_key': key, 'study_time': study, 'duration_minutes': minutes,
            'available_days': days or [], 'evidence_text': evidence.strip()}


HONEST = {
    STUDY_EVENING: cand('remember', 'preferred_study_time', study='evening', evidence=STUDY_EVENING),
    CHANGE_MORNING: cand('replace', 'preferred_study_time', study='morning', evidence=CHANGE_MORNING),
    DURATION_45: cand('remember', 'preferred_session_duration', minutes=45, evidence=DURATION_45),
    DAYS_MW: cand('remember', 'usual_available_days', days=['wed', 'mon'], evidence=DAYS_MW),
}


def propose(client, fake, content, reply=None):
    fake.reply_json = reply if reply is not None else HONEST.get(content)
    fake.reply_raw = None
    return client.post(PROPOSALS, json={'content': content})


def confirm(client, pid):
    return client.post(f'{PROPOSALS}/{pid}/confirm', json={})


def manual(client, key='preferred_study_time', value='evening', category='preference'):
    response = client.post(API, json={'category': category, 'key': key, 'value': value})
    assert response.status_code == 201, response.text
    return response.json()


def rows(tmp_path, table):
    with sqlite3.connect(tmp_path / 'test_tasks.db') as db:
        return db.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()


def snapshot(tmp_path, tables):
    return {t: rows(tmp_path, t) for t in tables}


def ready(client, fake, content):
    response = propose(client, fake, content)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'READY', body
    return body['proposal']


# ---------------------------------------------------------------- generation

def test_generate_create_preview_writes_nothing(client, fake_provider, store, tmp_path):
    before = snapshot(tmp_path, OTHER_TABLES + ['personal_memories', 'memory_confirmation_receipts'])
    response = propose(client, fake_provider, STUDY_EVENING)
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert body['status'] == 'READY' and body['code'] == 'MEMORY_PROPOSAL_READY'
    assert body['existing_memory'] is None
    proposal = body['proposal']
    assert uuid.UUID(proposal['proposal_id']).version == 4
    assert {k: v for k, v in proposal.items() if k != 'proposal_id'} == {
        'operation': 'CREATE', 'category': 'preference', 'key': 'preferred_study_time',
        'old_value': None, 'new_value': 'evening', 'target_memory_id': None, 'expected_version': None,
        'source_type': 'user_confirmed', 'expires_at': '2026-08-11T04:10:00Z',
    }
    assert snapshot(tmp_path, OTHER_TABLES + ['personal_memories', 'memory_confirmation_receipts']) == before
    assert len(store) == 1


def test_provider_receives_only_static_contract_and_current_sentence(client, fake_provider, store):
    marker = 'MEMORY_SENTINEL_10B_NEVER_SENT'
    manual(client, key='unrelated_fact', value=marker, category='stable_fact')
    manual(client, value='afternoon')
    cid = client.post('/api/conversations', json={'title': marker}).json()['id']
    assert cid
    propose(client, fake_provider, '  ' + CHANGE_MORNING + '  ')
    assert len(fake_provider.calls) == 1
    call = fake_provider.calls[0]
    assert call == [{'role': 'system', 'content': MEMORY_CANDIDATE_SYSTEM_PROMPT},
                    {'role': 'user', 'content': CHANGE_MORNING}]
    dumped = json.dumps(call, ensure_ascii=False)
    assert marker not in dumped and 'afternoon' not in dumped.split('Supported keys')[0]
    assert fake_provider.schemas_received[0] is service.MemoryCandidate


@pytest.mark.parametrize('content, status', [
    ("Don't remember that I study at night.", 'UNSUPPORTED'),
    ('My friend studies at night.', 'UNSUPPORTED'),
    ('The document says I should study for 45 minutes.', 'UNSUPPORTED'),
    ('I used to prefer evenings.', 'NEEDS_CLARIFICATION'),
    ('If I study, I prefer 45 minutes.', 'NEEDS_CLARIFICATION'),
    ("Remember that I don't like studying at night.", 'UNSUPPORTED'),
    ('I usually study in the evening.', 'NEEDS_CLARIFICATION'),
    ('I have a meeting tomorrow at 3 PM.', 'UNSUPPORTED'),
    ('Task 5 is done.', 'UNSUPPORTED'),
    ('Remind me to take medicine tonight.', 'UNSUPPORTED'),
    ('I might study tonight.', 'NEEDS_CLARIFICATION'),
    ('记住我偏好45分钟，同时通常周一有空', 'NEEDS_CLARIFICATION'),
    ('记住我喜欢每次学习999分钟', 'INVALID'),
    ('删除我的学习偏好', 'UNSUPPORTED'),
])
def test_frozen_edge_cases_via_api_even_with_adversarial_model(client, fake_provider, store, tmp_path, content, status):
    # The model claims a READY-looking candidate; deterministic policy still fails closed.
    adversarial = cand('remember', 'preferred_study_time', study='evening', evidence=content)
    response = propose(client, fake_provider, content, reply=adversarial)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    body = response.json()
    codes = {'UNSUPPORTED': 'MEMORY_PROPOSAL_UNSUPPORTED', 'NEEDS_CLARIFICATION': 'MEMORY_PROPOSAL_NEEDS_CLARIFICATION',
             'INVALID': 'MEMORY_PROPOSAL_INVALID'}
    assert body['status'] == status and body['code'] == codes[status]
    assert body['proposal'] is None and body['existing_memory'] is None
    assert content not in response.text
    assert fake_provider.calls == []  # never sent to the provider
    assert rows(tmp_path, 'personal_memories') == [] and len(store) == 0


@pytest.mark.parametrize('content', [STUDY_EVENING, DURATION_45, DAYS_MW])
def test_three_keys_create_and_confirm(client, fake_provider, store, tmp_path, content):
    proposal = ready(client, fake_provider, content)
    expected = {STUDY_EVENING: ('preferred_study_time', 'preference', 'evening'),
                DURATION_45: ('preferred_session_duration', 'preference', 45),
                DAYS_MW: ('usual_available_days', 'constraint', ['mon', 'wed'])}[content]
    assert (proposal['key'], proposal['category'], proposal['new_value']) == expected
    response = confirm(client, proposal['proposal_id'])
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    applied = response.json()
    assert applied == {'status': 'APPLIED', 'proposal_id': proposal['proposal_id'], 'operation': 'CREATE',
                       'memory_id': applied['memory_id'], 'applied_version': 1,
                       'confirmed_at': '2026-08-11T04:00:00Z', 'replayed': False}
    memory = client.get(f'{API}/{applied["memory_id"]}').json()
    assert memory['key'] == expected[0] and memory['value'] == expected[2]
    assert type(memory['value']) is type(expected[2])
    assert memory['source_type'] == 'user_confirmed' and memory['version'] == 1
    assert len(store) == 0


# ------------------------------------------------------- CREATE / UPDATE policy

def test_already_saved_is_a_true_noop(client, fake_provider, store, tmp_path):
    existing = manual(client, value='evening')
    before = snapshot(tmp_path, ['personal_memories', 'memory_confirmation_receipts'])
    response = propose(client, fake_provider, STUDY_EVENING)
    body = response.json()
    assert body['status'] == 'ALREADY_SAVED' and body['code'] == 'MEMORY_ALREADY_SAVED'
    assert body['proposal'] is None and body['existing_memory'] == existing
    assert snapshot(tmp_path, ['personal_memories', 'memory_confirmation_receipts']) == before
    assert existing['source_type'] == 'manual' and len(store) == 0
    # Explicit replacement with the same value is also a no-op.
    assert propose(client, fake_provider, 'Change my preferred study time to the evening.',
                   reply=cand('replace', 'preferred_study_time', study='evening',
                              evidence='Change my preferred study time to the evening.')).json()['status'] == 'ALREADY_SAVED'
    assert snapshot(tmp_path, ['personal_memories', 'memory_confirmation_receipts']) == before


def test_update_preview_confirm_and_source_transitions(client, fake_provider, store):
    existing = manual(client, value='evening')
    proposal = ready(client, fake_provider, CHANGE_MORNING)
    assert proposal['operation'] == 'UPDATE' and proposal['old_value'] == 'evening'
    assert proposal['new_value'] == 'morning' and proposal['target_memory_id'] == existing['id']
    assert proposal['expected_version'] == 1
    applied = confirm(client, proposal['proposal_id']).json()
    assert applied['operation'] == 'UPDATE' and applied['applied_version'] == 2 and not applied['replayed']
    memory = client.get(f'{API}/{existing["id"]}').json()
    assert (memory['value'], memory['version'], memory['source_type']) == ('morning', 2, 'user_confirmed')
    assert memory['created_at'] == existing['created_at']
    # Manual UPDATE always resets provenance to manual.
    edited = client.put(f'{API}/{existing["id"]}', json={'category': 'preference', 'key': 'preferred_study_time',
                                                         'value': 'afternoon', 'expected_version': 2}).json()
    assert edited['source_type'] == 'manual' and edited['version'] == 3


def test_replace_when_absent_creates(client, fake_provider, store):
    proposal = ready(client, fake_provider, CHANGE_MORNING)
    assert proposal['operation'] == 'CREATE' and proposal['old_value'] is None


def test_remember_with_different_existing_value_needs_clarification(client, fake_provider, store, tmp_path):
    manual(client, value='morning')
    before = rows(tmp_path, 'personal_memories')
    body = propose(client, fake_provider, STUDY_EVENING).json()
    assert body['status'] == 'NEEDS_CLARIFICATION' and body['proposal'] is None
    assert rows(tmp_path, 'personal_memories') == before and len(store) == 0


@pytest.mark.parametrize('content, key, category, value', [
    (STUDY_EVENING, 'preferred_study_time', 'preference', 'evenings'),          # legacy free-form
    (STUDY_EVENING, 'preferred_study_time', 'constraint', 'evening'),           # category mismatch
    # 45.0 is not usable here: SQLite NUMERIC affinity of the pre-existing 10A JSON
    # column stores it as integer 45 (a known 10A limitation, not changed in 10B).
    (DURATION_45, 'preferred_session_duration', 'preference', 45.5),            # float
    (DURATION_45, 'preferred_session_duration', 'preference', True),            # bool
    (DURATION_45, 'preferred_session_duration', 'preference', '45'),            # numeric string
    (DAYS_MW, 'usual_available_days', 'constraint', ['wed', 'mon']),            # not normalized
    (DAYS_MW, 'usual_available_days', 'constraint', 'mon,wed'),                 # wrong shape
    (CHANGE_MORNING, 'preferred_study_time', 'preference', {'time': 'evening'}),
])
def test_incompatible_existing_value_needs_clarification_without_migration(
        client, fake_provider, store, tmp_path, content, key, category, value):
    manual(client, key=key, value=value, category=category)
    before = rows(tmp_path, 'personal_memories')
    body = propose(client, fake_provider, content).json()
    assert body['status'] == 'NEEDS_CLARIFICATION' and body['proposal'] is None
    assert rows(tmp_path, 'personal_memories') == before


# ----------------------------------------------------------------- conflicts

def test_create_conflict_when_key_created_before_confirm(client, fake_provider, store, tmp_path):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    manual(client, value='morning')
    response = confirm(client, proposal['proposal_id'])
    assert response.status_code == 409 and response.json()['error']['code'] == 'MEMORY_KEY_CONFLICT'
    assert response.headers['cache-control'] == 'no-store'
    assert rows(tmp_path, 'memory_confirmation_receipts') == []
    assert confirm(client, proposal['proposal_id']).status_code == 410  # invalidated


@pytest.mark.parametrize('new_value', ['afternoon', 'morning'])
def test_update_version_conflict_even_if_latest_equals_proposal(client, fake_provider, store, tmp_path, new_value):
    existing = manual(client, value='evening')
    proposal = ready(client, fake_provider, CHANGE_MORNING)
    client.put(f'{API}/{existing["id"]}', json={'category': 'preference', 'key': 'preferred_study_time',
                                                'value': new_value, 'expected_version': 1})
    response = confirm(client, proposal['proposal_id'])
    assert response.status_code == 409 and response.json()['error']['code'] == 'MEMORY_VERSION_CONFLICT'
    memory = client.get(f'{API}/{existing["id"]}').json()
    assert (memory['value'], memory['version'], memory['source_type']) == (new_value, 2, 'manual')
    assert rows(tmp_path, 'memory_confirmation_receipts') == []
    assert confirm(client, proposal['proposal_id']).status_code == 410


def test_update_binding_changed_is_conflict(client, fake_provider, store):
    existing = manual(client, value='evening')
    proposal = ready(client, fake_provider, CHANGE_MORNING)
    client.put(f'{API}/{existing["id"]}', json={'category': 'preference', 'key': 'renamed_key',
                                                'value': 'evening', 'expected_version': 1})
    assert confirm(client, proposal['proposal_id']).json()['error']['code'] == 'MEMORY_VERSION_CONFLICT'


def test_update_target_deleted_and_same_key_recreated(client, fake_provider, store, tmp_path):
    existing = manual(client, value='evening')
    proposal = ready(client, fake_provider, CHANGE_MORNING)
    client.delete(f'{API}/{existing["id"]}')
    recreated = manual(client, value='evening')
    assert recreated['id'] != existing['id']
    response = confirm(client, proposal['proposal_id'])
    assert response.status_code == 404 and response.json()['error']['code'] == 'MEMORY_TARGET_NOT_FOUND'
    assert client.get(f'{API}/{recreated["id"]}').json() == recreated
    assert rows(tmp_path, 'memory_confirmation_receipts') == []


def test_update_target_deleted(client, fake_provider, store):
    existing = manual(client, value='evening')
    proposal = ready(client, fake_provider, CHANGE_MORNING)
    client.delete(f'{API}/{existing["id"]}')
    assert confirm(client, proposal['proposal_id']).json()['error']['code'] == 'MEMORY_TARGET_NOT_FOUND'
    assert client.get(API).json() == []


def test_competing_proposals_only_first_applies(client, fake_provider, store, tmp_path):
    first = ready(client, fake_provider, STUDY_EVENING)
    second = ready(client, fake_provider, STUDY_EVENING)
    assert confirm(client, first['proposal_id']).status_code == 200
    assert confirm(client, second['proposal_id']).json()['error']['code'] == 'MEMORY_KEY_CONFLICT'
    existing = client.get(API).json()[0]
    upd1 = ready(client, fake_provider, CHANGE_MORNING)
    upd2 = ready(client, fake_provider, CHANGE_MORNING)
    assert upd1['target_memory_id'] == upd2['target_memory_id'] == existing['id']
    assert confirm(client, upd1['proposal_id']).status_code == 200
    assert confirm(client, upd2['proposal_id']).json()['error']['code'] == 'MEMORY_VERSION_CONFLICT'
    assert len(rows(tmp_path, 'memory_confirmation_receipts')) == 2


# ------------------------------------------------------- idempotency / replay

def test_receipt_is_minimal_and_survives_delete_and_update(client, fake_provider, store, tmp_path, engine):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    applied = confirm(client, proposal['proposal_id']).json()
    columns = {c['name'] for c in inspect(engine).get_columns('memory_confirmation_receipts')}
    assert columns == {'proposal_id', 'operation', 'memory_id', 'applied_version', 'status', 'confirmed_at'}
    assert inspect(engine).get_foreign_keys('memory_confirmation_receipts') == []
    receipt_rows = rows(tmp_path, 'memory_confirmation_receipts')
    assert len(receipt_rows) == 1 and receipt_rows[0][0] == proposal['proposal_id']
    assert 'evening' not in json.dumps(receipt_rows) and 'preferred_study_time' not in json.dumps(receipt_rows)
    # Replay after manual update: historical result, Memory untouched.
    client.put(f'{API}/{applied["memory_id"]}', json={'category': 'preference', 'key': 'preferred_study_time',
                                                      'value': 'afternoon', 'expected_version': 1})
    after_update = client.get(f'{API}/{applied["memory_id"]}').json()
    replay = confirm(client, proposal['proposal_id']).json()
    assert replay == {**applied, 'replayed': True}
    assert client.get(f'{API}/{applied["memory_id"]}').json() == after_update
    # Replay after delete never recreates the Memory.
    assert client.delete(f'{API}/{applied["memory_id"]}').status_code == 200
    assert confirm(client, proposal['proposal_id']).json() == {**applied, 'replayed': True}
    assert client.get(API).json() == [] and len(rows(tmp_path, 'memory_confirmation_receipts')) == 1
    # Cancel of an applied proposal returns the historical result and deletes nothing.
    cancel = client.delete(f'{PROPOSALS}/{proposal["proposal_id"]}')
    assert cancel.json() == {**applied, 'replayed': True} and cancel.headers['cache-control'] == 'no-store'


def test_restart_loses_pending_but_receipt_replay_survives(client, fake_provider, store, mono):
    applied_proposal = ready(client, fake_provider, STUDY_EVENING)
    applied = confirm(client, applied_proposal['proposal_id']).json()
    pending = ready(client, fake_provider, DURATION_45)
    restarted = MemoryProposalStore(monotonic=mono)
    app.dependency_overrides[get_memory_proposal_store] = lambda: restarted
    response = confirm(client, pending['proposal_id'])
    assert response.status_code == 410 and response.json()['error']['code'] == 'MEMORY_PROPOSAL_EXPIRED'
    assert confirm(client, applied_proposal['proposal_id']).json() == {**applied, 'replayed': True}
    assert len(client.get(API).json()) == 1


def test_ttl_expiry_on_access_and_receipt_first(client, fake_provider, store, mono):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    mono.t += 599.9
    assert store.get_valid(proposal['proposal_id']) is not None
    mono.t += 0.1
    response = confirm(client, proposal['proposal_id'])
    assert response.status_code == 410 and len(store) == 0
    assert client.get(API).json() == []
    # A confirmed proposal remains replayable long after its TTL.
    second = ready(client, fake_provider, STUDY_EVENING)
    applied = confirm(client, second['proposal_id']).json()
    mono.t += 10_000
    assert confirm(client, second['proposal_id']).json() == {**applied, 'replayed': True}


def test_unknown_and_invalid_proposal_ids(client, store):
    unknown = str(uuid.uuid4())
    response = confirm(client, unknown)
    assert response.status_code == 410 and response.json()['error']['code'] == 'MEMORY_PROPOSAL_EXPIRED'
    assert client.delete(f'{PROPOSALS}/{unknown}').json() == {'status': 'CANCELLED', 'proposal_id': unknown}
    for bad in ['not-a-uuid', str(uuid.uuid4()).upper(), str(uuid.uuid1()), '0' * 36]:
        for response in (confirm(client, bad), client.delete(f'{PROPOSALS}/{bad}')):
            assert response.status_code == 422 and response.json()['error']['code'] == 'VALIDATION_ERROR'
            assert response.headers['cache-control'] == 'no-store'


def test_cancel_pending_then_confirm_is_expired(client, fake_provider, store, tmp_path):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    cancel = client.delete(f'{PROPOSALS}/{proposal["proposal_id"]}')
    assert cancel.status_code == 200 and cancel.json() == {'status': 'CANCELLED', 'proposal_id': proposal['proposal_id']}
    assert cancel.headers['cache-control'] == 'no-store'
    assert client.delete(f'{PROPOSALS}/{proposal["proposal_id"]}').json()['status'] == 'CANCELLED'
    assert confirm(client, proposal['proposal_id']).status_code == 410
    assert rows(tmp_path, 'personal_memories') == [] and rows(tmp_path, 'memory_confirmation_receipts') == []


def test_capacity_rejects_without_evicting_valid_proposals(client, fake_provider, store, mono):
    kept = ready(client, fake_provider, STUDY_EVENING)
    for _ in range(store.capacity - 1):
        store.add(PendingMemoryProposal(str(uuid.uuid4()), 'CREATE', 'k', 'preference', 'v', None, None, None,
                                        FIXED, FIXED, store.expiry_deadline()))
    response = propose(client, fake_provider, DURATION_45)
    assert response.status_code == 503 and response.headers['cache-control'] == 'no-store'
    assert response.json()['error']['code'] == 'MEMORY_PROPOSAL_CAPACITY_EXCEEDED'
    assert store.get_valid(kept['proposal_id']) is not None and len(store) == 128
    mono.t += 600
    assert ready(client, fake_provider, DURATION_45)['operation'] == 'CREATE'
    assert len(store) == 1


def test_periodic_cleanup_thread(mono):
    s = MemoryProposalStore(monotonic=mono)
    s.add(PendingMemoryProposal(str(uuid.uuid4()), 'CREATE', 'k', 'preference', 'v', None, None, None,
                                FIXED, FIXED, s.expiry_deadline()))
    s.start_cleanup(interval_seconds=0.01)
    try:
        mono.t += 600
        done = threading.Event()
        for _ in range(500):
            if len(s) == 0:
                done.set()
                break
            done.wait(0.01)
        assert done.is_set()
    finally:
        s.stop_cleanup()


# --------------------------------------------------- faults / races / loss

def test_fault_before_commit_rolls_back_and_keeps_same_proposal(client, fake_provider, store, tmp_path, monkeypatch):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    original = Session.commit

    def failing_commit(self):
        raise OperationalError('COMMIT', {}, Exception('disk I/O error'))

    monkeypatch.setattr(Session, 'commit', failing_commit)
    response = confirm(client, proposal['proposal_id'])
    assert response.status_code == 503 and response.json()['error']['code'] == 'MEMORY_STORAGE_ERROR'
    assert 'disk' not in response.text and 'evening' not in response.text
    assert rows(tmp_path, 'personal_memories') == [] and rows(tmp_path, 'memory_confirmation_receipts') == []
    monkeypatch.setattr(Session, 'commit', original)
    retry = confirm(client, proposal['proposal_id'])
    assert retry.status_code == 200 and retry.json()['replayed'] is False
    assert len(rows(tmp_path, 'personal_memories')) == 1


def test_response_loss_after_commit_retry_replays(client, fake_provider, store, engine):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    with Session(engine) as db:  # commit happens, response "lost"
        lost = service.confirm_proposal(db, store, FixedClock(FIXED), proposal['proposal_id'])
    assert lost.replayed is False
    retry = confirm(client, proposal['proposal_id']).json()
    assert retry['replayed'] is True and retry['memory_id'] == lost.memory_id
    memories = client.get(API).json()
    assert len(memories) == 1 and memories[0]['version'] == 1


def test_receipt_insert_race_reads_committed_receipt_without_rerunning_mutation(
        client, fake_provider, store, engine, tmp_path, monkeypatch):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    with Session(engine) as other:
        other.add(MemoryConfirmationReceipt(proposal_id=proposal['proposal_id'], operation='CREATE', memory_id=77,
                                            applied_version=1, status='APPLIED', confirmed_at=FIXED))
        other.commit()
    real = service._read_receipt
    calls = []

    def racing_read(db, pid):
        calls.append(pid)
        return None if len(calls) == 1 else real(db, pid)

    monkeypatch.setattr(service, '_read_receipt', racing_read)
    with Session(engine) as db:
        result = service.confirm_proposal(db, store, FixedClock(FIXED), proposal['proposal_id'])
    assert result.replayed is True and result.memory_id == 77
    assert rows(tmp_path, 'personal_memories') == []  # our mutation rolled back, not rerun
    assert len(store) == 0


def test_concurrent_same_id_confirm_applies_once(client, fake_provider, store, engine, tmp_path):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    barrier = threading.Barrier(4)

    def run(_):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            return service.confirm_proposal(db, store, FixedClock(FIXED), proposal['proposal_id']).replayed

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(run, range(4)))
    assert sorted(outcomes) == [False, True, True, True]
    assert len(rows(tmp_path, 'personal_memories')) == 1
    assert len(rows(tmp_path, 'memory_confirmation_receipts')) == 1


def test_cancel_vs_confirm_race_is_consistent(client, fake_provider, store, engine, tmp_path):
    for _ in range(5):
        proposal = ready(client, fake_provider, STUDY_EVENING)
        barrier = threading.Barrier(2)

        def do_confirm():
            with Session(engine) as db:
                barrier.wait(timeout=10)
                try:
                    return service.confirm_proposal(db, store, FixedClock(FIXED), proposal['proposal_id']).status
                except MemoryError as exc:
                    return exc.code

        def do_cancel():
            with Session(engine) as db:
                barrier.wait(timeout=10)
                return service.cancel_proposal(db, store, proposal['proposal_id']).status

        with ThreadPoolExecutor(max_workers=2) as pool:
            confirmed, cancelled = pool.submit(do_confirm), pool.submit(do_cancel)
            outcome = (confirmed.result(), cancelled.result())
        memories = rows(tmp_path, 'personal_memories')
        if outcome == ('APPLIED', 'APPLIED'):
            assert len(memories) == 1
            with sqlite3.connect(tmp_path / 'test_tasks.db') as db:
                db.execute('DELETE FROM personal_memories')
        else:
            assert outcome == ('MEMORY_PROPOSAL_EXPIRED', 'CANCELLED') and memories == []


# ------------------------------------------------------ model output strictness

BASE = cand('remember', 'preferred_study_time', study='evening', evidence=STUDY_EVENING)


@pytest.mark.parametrize('raw', [
    'not json',
    '```json\n' + json.dumps(BASE) + '\n```',
    json.dumps(BASE) + json.dumps(BASE),
    json.dumps([BASE]),
    '{"intent":"remember","intent":"remember","canonical_key":"preferred_study_time","study_time":"evening",'
    '"duration_minutes":0,"available_days":[],"evidence_text":"' + STUDY_EVENING + '"}',
    json.dumps(BASE).replace('"duration_minutes": 0', '"duration_minutes": NaN'),
    json.dumps(BASE).replace('"duration_minutes": 0', '"duration_minutes": Infinity'),
    json.dumps({**BASE, 'evidence_text': STUDY_EVENING + ' '}),
    json.dumps({**BASE, 'evidence_text': 'Remember that I usually study in the evening'}),
    json.dumps({**BASE, 'padding': 'x' * 9000}),
])
def test_malformed_model_output_is_invalid(client, fake_provider, store, tmp_path, raw):
    fake_provider.reply_json = None
    fake_provider.reply_raw = raw
    response = client.post(PROPOSALS, json={'content': STUDY_EVENING})
    body = response.json()
    assert response.status_code == 200 and body['status'] == 'INVALID' and body['proposal'] is None
    assert 'x' * 50 not in response.text and 'NaN' not in response.text
    assert rows(tmp_path, 'personal_memories') == [] and len(store) == 0


@pytest.mark.parametrize('changes', [
    {'memory_id': 5}, {'id': 5}, {'version': 2}, {'expected_version': 1}, {'source_type': 'user_confirmed'},
    {'category': 'preference'}, {'operation': 'CREATE'}, {'proposal_id': str(uuid.uuid4())},
    {'confirmed': True}, {'status': 'APPLIED'}, {'tool': 'memory.write'}, {'function_call': 'save'},
    {'message': 'saved'}, {'saved': True},
])
def test_forged_fields_are_rejected(client, fake_provider, store, tmp_path, changes):
    body = propose(client, fake_provider, STUDY_EVENING, reply={**BASE, **changes}).json()
    assert body['status'] == 'INVALID'
    assert rows(tmp_path, 'personal_memories') == [] and rows(tmp_path, 'memory_confirmation_receipts') == []


@pytest.mark.parametrize('reply', [
    cand('remember', 'preferred_session_duration', minutes=True, evidence=DURATION_45),
    cand('remember', 'preferred_session_duration', minutes=45.0, evidence=DURATION_45),
    cand('remember', 'preferred_session_duration', minutes='45', evidence=DURATION_45),
    cand('remember', 'preferred_session_duration', minutes=45, study='evening', evidence=DURATION_45),
    cand('remember', 'preferred_session_duration', minutes=0, evidence=DURATION_45),
    cand('clarify', 'preferred_session_duration', evidence=DURATION_45),
    cand('unsupported', minutes=45, evidence=DURATION_45),
    cand('remember', 'arbitrary_key', minutes=45, evidence=DURATION_45),
    {k: v for k, v in cand('remember', 'preferred_session_duration', minutes=45, evidence=DURATION_45).items()
     if k != 'available_days'},
])
def test_schema_and_slot_violations_are_invalid(client, fake_provider, store, reply):
    assert propose(client, fake_provider, DURATION_45, reply=reply).json()['status'] == 'INVALID'
    assert len(store) == 0


@pytest.mark.parametrize('content, reply', [
    # A. intent disagreement
    (STUDY_EVENING, cand('replace', 'preferred_study_time', study='evening', evidence=STUDY_EVENING)),
    (CHANGE_MORNING, cand('remember', 'preferred_study_time', study='morning', evidence=CHANGE_MORNING)),
    # B. canonical key disagreement
    (DURATION_45, cand('remember', 'preferred_study_time', study='evening', evidence=DURATION_45)),
    (STUDY_EVENING, cand('remember', 'usual_available_days', days=['mon'], evidence=STUDY_EVENING)),
    # C. canonical value disagreement
    (DURATION_45, cand('remember', 'preferred_session_duration', minutes=60, evidence=DURATION_45)),
    (STUDY_EVENING, cand('remember', 'preferred_study_time', study='morning', evidence=STUDY_EVENING)),
    (DAYS_MW, cand('remember', 'usual_available_days', days=['mon'], evidence=DAYS_MW)),
    # D. model returns clarify for an unambiguous frozen form
    (STUDY_EVENING, cand('clarify', evidence=STUDY_EVENING)),
    (DURATION_45, cand('clarify', evidence=DURATION_45)),
    # E. model returns unsupported for an unambiguous frozen form
    (CHANGE_MORNING, cand('unsupported', evidence=CHANGE_MORNING)),
    (DAYS_MW, cand('unsupported', evidence=DAYS_MW)),
])
def test_model_disagreement_with_frozen_grammar_is_invalid(client, fake_provider, store, tmp_path, content, reply):
    # Pre-existing state that must stay untouched: one Memory + receipt, one pending proposal.
    seed_text = 'Remember that I am usually available on Friday.'
    fake_provider.reply_json = cand('remember', 'usual_available_days', days=['fri'], evidence=seed_text)
    applied = confirm(client, client.post(PROPOSALS, json={'content': seed_text}).json()['proposal']['proposal_id'])
    assert applied.status_code == 200
    pending_text = 'Change my preferred session duration to 90 minutes.'
    fake_provider.reply_json = cand('replace', 'preferred_session_duration', minutes=90, evidence=pending_text)
    assert client.post(PROPOSALS, json={'content': pending_text}).json()['status'] == 'READY'
    before = snapshot(tmp_path, ['personal_memories', 'memory_confirmation_receipts'])
    pending_before, calls_before = len(store), len(fake_provider.calls)

    response = propose(client, fake_provider, content, reply=reply)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert body == {'status': 'INVALID', 'code': 'MEMORY_PROPOSAL_INVALID',
                    'message': 'The model result did not match the supported input. Please generate the proposal again.',
                    'proposal': None, 'existing_memory': None}
    assert content not in response.text
    assert len(store) == pending_before == 1                      # no new pending proposal
    assert snapshot(tmp_path, ['personal_memories', 'memory_confirmation_receipts']) == before
    assert len(fake_provider.calls) == calls_before + 1           # exactly one call, no repair/retry


@pytest.mark.parametrize('content, intent', [
    ('REMEMBER THAT I AM USUALLY AVAILABLE ON MONDAY AND WEDNESDAY.', 'remember'),
    ('Remember that I am usually available on MONDAY AND WEDNESDAY.', 'remember'),
    ('remember that i am usually available on monday AND wednesday', 'remember'),
    ('Change my usual available days to Monday And Wednesday.', 'replace'),
    ('Remember that I am usually available on MON and Wed', 'remember'),
])
def test_uppercase_and_mixed_case_english_days_reach_ready(client, fake_provider, store, tmp_path, content, intent):
    """Astra R1: previously KeyError -> HTTP 500; now one normal provider call and READY."""
    reply = cand(intent, 'usual_available_days', days=['mon', 'wed'], evidence=content)
    response = propose(client, fake_provider, content, reply=reply)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'READY', body
    proposal = body['proposal']
    assert proposal['key'] == 'usual_available_days' and proposal['new_value'] == ['mon', 'wed']
    assert proposal['category'] == 'constraint' and proposal['operation'] == 'CREATE'
    assert fake_provider.calls == [[{'role': 'system', 'content': MEMORY_CANDIDATE_SYSTEM_PROMPT},
                                     {'role': 'user', 'content': content.strip()}]]
    assert rows(tmp_path, 'personal_memories') == [] and len(store) == 1


@pytest.mark.parametrize('content', [
    'Remember that I am usually available on ſunday.',
    'Remember that I am usually available on Wedneſday.',
    'Remember that I am usually available on Frıday',
    'Change my usual available days to mon, ſat',
    'Remember that ı usually study in the evening.',
    'Remember that I usually study in the Korning',
])
def test_unicode_case_fold_variants_fail_closed_without_llm(client, fake_provider, store, tmp_path, content):
    """Astra R1: previously KeyError -> HTTP 500; now a deterministic rejection before the LLM."""
    adversarial = cand('remember', 'usual_available_days', days=['sun'], evidence=content)
    response = propose(client, fake_provider, content, reply=adversarial)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'NEEDS_CLARIFICATION' and body['code'] == 'MEMORY_PROPOSAL_NEEDS_CLARIFICATION'
    assert body['proposal'] is None and body['existing_memory'] is None
    assert fake_provider.calls == [] and len(store) == 0
    assert rows(tmp_path, 'personal_memories') == [] and rows(tmp_path, 'memory_confirmation_receipts') == []


def test_model_day_list_is_normalized_before_comparison(client, fake_provider, store):
    reply = cand('remember', 'usual_available_days', days=['wed', 'mon', 'wed'], evidence=DAYS_MW)
    assert propose(client, fake_provider, DAYS_MW, reply=reply).json()['proposal']['new_value'] == ['mon', 'wed']


@pytest.mark.parametrize('error, status, code', [
    (LLMTimeoutError('slow'), 502, 'LLM_TIMEOUT'),
    (LLMUpstreamError('boom'), 502, 'LLM_UPSTREAM_ERROR'),
])
def test_provider_errors_are_safe(client, fake_provider, store, tmp_path, error, status, code):
    fake_provider.error = error
    response = client.post(PROPOSALS, json={'content': STUDY_EVENING})
    assert response.status_code == status and response.json()['error']['code'] == code
    assert response.headers['cache-control'] == 'no-store'
    assert rows(tmp_path, 'personal_memories') == [] and len(store) == 0


def test_provider_not_configured(client, store):
    def unconfigured():
        raise LLMNotConfiguredError('missing')
    app.dependency_overrides[get_llm_provider] = unconfigured
    response = client.post(PROPOSALS, json={'content': STUDY_EVENING})
    assert response.status_code == 503 and response.json()['error']['code'] == 'LLM_NOT_CONFIGURED'
    assert response.headers['cache-control'] == 'no-store'


def test_confirm_never_calls_the_llm(client, fake_provider, store):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    calls = len(fake_provider.calls)
    fake_provider.error = LLMUpstreamError('must not be called')
    assert confirm(client, proposal['proposal_id']).status_code == 200
    assert len(fake_provider.calls) == calls


# ------------------------------------------------------------ request contract

@pytest.mark.parametrize('payload', [
    {}, {'content': ''}, {'content': '   '}, {'content': 123}, {'content': None},
    {'content': 'x' * 1001}, {'content': ' ' * 200 + '😀' * 1000},
    {'content': 'line one\nline two'}, {'content': 'a\rb'}, {'content': 'a\x00b'}, {'content': 'a b'},
    {'content': 'tab\there'},
    {'content': STUDY_EVENING, 'source_type': 'manual'}, {'content': STUDY_EVENING, 'key': 'x'},
    {'content': STUDY_EVENING, 'operation': 'CREATE'}, {'content': STUDY_EVENING, 'expected_version': 1},
])
def test_generation_request_validation(client, fake_provider, store, payload):
    response = client.post(PROPOSALS, json=payload)
    assert response.status_code == 422 and response.json()['error']['code'] == 'VALIDATION_ERROR'
    assert response.headers['cache-control'] == 'no-store'
    assert fake_provider.calls == [] and len(store) == 0


def test_generation_accepts_boundary_lengths(client, fake_provider, store):
    long_sentence = '记住我通常晚上学习' + ' ' * 991
    assert len(long_sentence) == 1000
    reply = cand('remember', 'preferred_study_time', study='evening', evidence=long_sentence)
    assert propose(client, fake_provider, long_sentence, reply=reply).json()['status'] == 'READY'


@pytest.mark.parametrize('body', [{'value': 'x'}, {'expected_version': 1}, {'operation': 'CREATE'},
                                  {'source_type': 'manual'}, {'proposal_id': 'x'}])
def test_confirm_rejects_client_supplied_fields(client, fake_provider, store, tmp_path, body):
    proposal = ready(client, fake_provider, STUDY_EVENING)
    response = client.post(f'{PROPOSALS}/{proposal["proposal_id"]}/confirm', json=body)
    assert response.status_code == 422 and response.headers['cache-control'] == 'no-store'
    assert client.post(f'{PROPOSALS}/{proposal["proposal_id"]}/confirm').status_code == 422
    assert rows(tmp_path, 'personal_memories') == [] and len(store) == 1


def test_no_proposal_listing_endpoint_and_manual_routes_intact(client, fake_provider, store):
    assert client.get(PROPOSALS).status_code in (404, 405, 422)
    ready(client, fake_provider, STUDY_EVENING)
    assert client.get(PROPOSALS).status_code in (404, 405, 422)
    item = manual(client, key='other_key', value='x')
    assert client.get(f'{API}/{item["id"]}').status_code == 200
    assert client.delete(f'{API}/{item["id"]}').json() == {'deleted': True}


# ------------------------------------------------------- privacy / isolation

def test_no_sentence_value_or_model_json_in_logs(client, fake_provider, store, caplog):
    caplog.set_level(logging.DEBUG)
    proposal = ready(client, fake_provider, DURATION_45)
    confirm(client, proposal['proposal_id'])
    propose(client, fake_provider, 'My friend studies at night SENTINEL_LOG_10B.')
    text = caplog.text
    assert DURATION_45 not in text and 'SENTINEL_LOG_10B' not in text
    assert 'evidence_text' not in text and '"duration_minutes"' not in text


def test_generate_and_confirm_touch_only_memory_tables(client, fake_provider, store, tmp_path):
    before = snapshot(tmp_path, OTHER_TABLES)
    proposal = ready(client, fake_provider, STUDY_EVENING)
    confirm(client, proposal['proposal_id'])
    assert snapshot(tmp_path, OTHER_TABLES) == before
    assert len(rows(tmp_path, 'personal_memories')) == 1


@pytest.mark.parametrize('flow', ['chat', 'action', 'rag'])
def test_other_paths_never_write_memory_or_receipts(client, fake_provider, store, tmp_path, flow):
    cid = client.post('/api/conversations', json={'title': 'boundary'}).json()['id']
    if flow == 'chat':
        fake_provider.reply = 'Saved! I will remember that you study in the evening.'
        assert client.post(f'/api/conversations/{cid}/messages', json={'content': STUDY_EVENING}).status_code == 201
    elif flow == 'action':
        fake_provider.reply_json = {'intent': 'unsupported', 'task_title': None, 'task_due_text': None,
                                    'reminder_time_text': None, 'explanation': 'saved'}
        response = client.post(f'/api/conversations/{cid}/action-proposals',
                               json={'content': '记住我通常晚上学习', 'timezone': 'Asia/Shanghai'})
        assert response.status_code == 200
    else:
        doc = client.post('/api/documents', files={'file': ('m.txt', b'I usually study in the evening.', 'text/plain')}).json()
        client.post(f'/api/retrieval/index/{doc["id"]}')
        fake_provider.reply_json = {'answer': 'Saved to memory.', 'cited_labels': ['C1'], 'sufficient_evidence': True}
        assert client.post('/api/rag/ask', json={'question': 'When do I study?'}).status_code == 200
    assert rows(tmp_path, 'personal_memories') == [] and rows(tmp_path, 'memory_confirmation_receipts') == []
    assert len(store) == 0


def test_source_type_check_constraint(client, tmp_path):
    with sqlite3.connect(tmp_path / 'test_tasks.db') as db:
        for source in ('manual', 'user_confirmed'):
            db.execute("INSERT INTO personal_memories(category,key,value_json,source_type,version,created_at,updated_at) "
                       "VALUES('preference',?,'1',?,1,'2026-01-01','2026-01-01')", (f'k_{source}', source))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO personal_memories(category,key,value_json,source_type,version,created_at,updated_at) "
                       "VALUES('preference','k_llm','1','llm_generated',1,'2026-01-01','2026-01-01')")
    assert {m['source_type'] for m in client.get(API).json()} == {'manual', 'user_confirmed'}


# ------------------------------------ SQLite numeric affinity (pre-existing limitation)

def test_type_sensitive_comparison_distinguishes_int_and_float_when_both_types_present():
    from app.services.memory_grammar import is_canonical_value, same_value
    assert same_value(45, 45) and not same_value(45, 45.0) and not same_value(45.0, 45)
    assert not same_value(45, True) and not same_value(45, '45')
    assert is_canonical_value('preferred_session_duration', 45)
    for bad in (45.0, True, '45'):
        assert not is_canonical_value('preferred_session_duration', bad)


def test_observed_sqlite_round_trip_of_manual_float_is_integer(client, fake_provider, store, tmp_path):
    """Observed storage round-trip behaviour, NOT proof that JSON numeric type is preserved.

    The pre-existing 10A `value_json` column is declared JSON, which SQLite gives NUMERIC
    affinity: a manually stored 45.0 is read back as integer 45. Phase 10B compares the
    ACTUAL read value; it cannot reconstruct type information SQLite already lost.
    """
    created = manual(client, key='preferred_session_duration', value=45.0)
    assert type(created['value']) is float  # the create response echoes the validated input
    with sqlite3.connect(tmp_path / 'test_tasks.db') as db:
        assert db.execute('SELECT typeof(value_json) FROM personal_memories').fetchone() == ('integer',)
    read = client.get(f'{API}/{created["id"]}').json()['value']
    assert read == 45 and type(read) is int
    body = propose(client, fake_provider, DURATION_45).json()
    assert body['status'] == 'ALREADY_SAVED' and body['existing_memory']['value'] == 45


def test_currently_read_float_still_fails_integer_duration_contract(client, fake_provider, store, monkeypatch):
    manual(client, key='preferred_session_duration', value=45)
    real = service.memory_out

    def as_float(row):
        out = real(row)
        return out.model_copy(update={'value': 45.0})

    monkeypatch.setattr(service, 'memory_out', as_float)
    body = propose(client, fake_provider, DURATION_45).json()
    assert body['status'] == 'NEEDS_CLARIFICATION' and body['proposal'] is None and len(store) == 0


def test_llm_candidate_float_duration_is_rejected(client, fake_provider, store):
    reply = cand('remember', 'preferred_session_duration', minutes=45.0, evidence=DURATION_45)
    assert propose(client, fake_provider, DURATION_45, reply=reply).json()['status'] == 'INVALID'
    assert len(store) == 0
