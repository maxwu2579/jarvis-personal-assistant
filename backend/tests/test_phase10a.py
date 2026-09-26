"""Manual memory contract, boundaries and real independent-session version checks."""

import json
import sqlite3
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.clock import FixedClock
from app.models.personal_memory import PersonalMemory
from app.schemas.memory import MemoryCreate, MemoryUpdate
from app.services.memory_service import MemoryError, update_memory

API = '/api/memories'


def create(client, **changes):
    return client.post(API, json={'category': 'preference', 'key': 'study_time', 'value': 'evenings', **changes})


def snapshot(tmp_path, *, memory_only=False):
    with sqlite3.connect(tmp_path / 'test_tasks.db') as db:
        names = ['personal_memories'] if memory_only else ['tasks', 'reminders', 'executions', 'execution_attempts', 'execution_steps', 'execution_events', 'documents', 'document_chunks', 'notifications', 'messages', 'conversations', 'rag_answers']
        return {t: db.execute(f'SELECT * FROM "{t}" ORDER BY id').fetchall() for t in names}


@pytest.mark.parametrize('category', ['preference', 'stable_fact', 'constraint'])
def test_create_categories_and_persist(client, tmp_path, category):
    response = create(client, category=category)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row['version'] == 1 and row['source_type'] == 'manual'
    assert row['created_at'] == row['updated_at'] == '2026-08-11T04:00:00Z'
    assert client.get(f'{API}/{row["id"]}').json() == row
    engine = create_engine(f'sqlite:///{tmp_path / "test_tasks.db"}')
    with Session(engine) as session:
        assert session.get(PersonalMemory, row['id']).value_json == 'evenings'
    engine.dispose()


@pytest.mark.parametrize('value', ['text', 45, 1.5, True, False, [], {}, ['晚间', 45, 1.5, True], {'time': '晚间', 'minutes': 45, 'enabled': False}, 'x' * 4094, list(range(64)), {str(i): i for i in range(32)}])
def test_valid_values(client, value):
    response = create(client, value=value)
    assert response.status_code == 201, response.text
    assert response.json()['value'] == value
    assert type(response.json()['value']) is type(value)


@pytest.mark.parametrize('changes', [
    {'category': 'task'}, {'category': 'calendar_event'}, {'category': 'execution'},
    {'key': 'Upper'}, {'key': 'a' * 81}, {'key': ''}, {'key': '<b>key</b>'}, {'key': 'a\n'}, {'key': 'a\x00'},
    {'value': None}, {'value': 'x' * 4095}, {'value': '中' * 1400},
    {'value': [[1]]}, {'value': {'nested': {}}}, {'value': [None]},
    {'value': list(range(65))}, {'value': {str(i): i for i in range(33)}},
    {'value': {'': 'empty name'}}, {'value': {'x\n': 1}},
    {'source_type': 'manual'}, {'source_type': 'llm_generated'}, {'version': 9},
    {'created_at': '2026-01-01'}, {'updated_at': '2026-01-01'},
    {'tool_name': 'run'}, {'handler': 'x'}, {'execution_type': 'x'}, {'sql': 'SELECT 1'},
])
def test_invalid_create_is_safe_and_has_no_effect(client, changes):
    response = create(client, **changes)
    assert response.status_code == 422, response.text
    assert response.json()['error']['code'] == 'VALIDATION_ERROR'
    assert 'Traceback' not in response.text
    assert client.get(API).json() == []


@pytest.mark.parametrize('number', ['NaN', 'Infinity', '-Infinity'])
def test_nonfinite_json_rejected(client, number):
    response = client.post(API, content='{"category":"preference","key":"number","value":' + number + '}', headers={'Content-Type': 'application/json'})
    assert response.status_code == 422, response.text
    assert client.get(API).json() == []


@pytest.mark.parametrize('value', [b'bytes', object(), ('tuple',), float('nan'), float('inf')])
def test_raw_python_objects_rejected(value):
    with pytest.raises(ValidationError):
        MemoryCreate(category='preference', key='value', value=value)


def test_list_sort_filter_duplicate(client):
    create(client, category='stable_fact', key='z')
    create(client, key='b')
    create(client, category='constraint', key='c')
    create(client, key='a')
    rows = client.get(API).json()
    assert [r['key'] for r in rows] == ['c', 'a', 'b', 'z']
    assert [r['key'] for r in client.get(API + '?category=preference').json()] == ['a', 'b']
    assert client.get(API + '?category=task').status_code == 422
    assert create(client, category='constraint', key='a').status_code == 409
    assert client.get(API).json() == rows


def test_update_conflicts_atomicity_and_delete(client, tmp_path):
    item = create(client).json(); path = f'{API}/{item["id"]}'
    payload = {'category': 'constraint', 'key': 'weekends', 'value': [True, 45], 'expected_version': 1}
    updated = client.put(path, json=payload)
    assert updated.status_code == 200, updated.text
    assert updated.json()['version'] == 2
    assert updated.json()['created_at'] == item['created_at']
    assert client.put(path, json=payload).json()['error']['code'] == 'MEMORY_VERSION_CONFLICT'
    create(client, key='taken')
    response = client.put(path, json={**payload, 'key': 'taken', 'expected_version': 2})
    assert response.status_code == 409 and response.json()['error']['code'] == 'MEMORY_KEY_CONFLICT'
    assert client.get(path).json() == updated.json()
    for version in [True, '2', 0, -1]:
        assert client.put(path, json={**payload, 'expected_version': version}).status_code == 422
    missing = dict(payload); missing.pop('expected_version')
    assert client.put(path, json=missing).status_code == 422
    assert client.put(path, json={**payload, 'expected_version': 2, 'source_type': 'manual'}).status_code == 422
    assert client.delete(path).status_code == 200
    assert client.get(path).status_code == 404
    assert client.delete(path).status_code == 404
    assert client.put(path, json=payload).status_code == 404
    with sqlite3.connect(tmp_path / 'test_tasks.db') as db:
        assert db.execute('SELECT COUNT(*) FROM personal_memories WHERE id=?', (item['id'],)).fetchone()[0] == 0
    assert all(r['id'] != item['id'] for r in client.get(API).json())
    assert create(client, key='weekends').status_code == 201


def test_independent_sessions_cannot_overwrite_newer_value(client, tmp_path):
    item = create(client).json()
    engine = create_engine(f'sqlite:///{tmp_path / "test_tasks.db"}')
    clock = FixedClock(datetime(2026, 9, 22, tzinfo=timezone.utc))
    with Session(engine) as first, Session(engine) as second:
        stale = second.get(PersonalMemory, item['id'])
        second.commit()  # End read transaction, retain the client's expected version.
        payload = MemoryUpdate(category='preference', key='study_time', value='new', expected_version=1)
        result = update_memory(first, item['id'], payload, clock)
        assert result.version == 2 and result.updated_at == clock.now().replace(tzinfo=None)
        with pytest.raises(MemoryError) as exc:
            update_memory(second, item['id'], payload.model_copy(update={'value': 'stale'}), clock)
        assert exc.value.code == 'MEMORY_VERSION_CONFLICT'
        assert second.scalar(select(PersonalMemory.value_json)) == 'new'
        assert stale is not None
    engine.dispose()


def test_deleted_id_is_not_reused_by_new_memory(client):
    original = create(client).json()
    path = f'{API}/{original["id"]}'
    assert client.delete(path).status_code == 200
    replacement = create(client).json()
    assert replacement['id'] != original['id']
    assert client.put(path, json={'category':'preference', 'key':'study_time', 'value':'stale', 'expected_version':1}).status_code == 404
    assert client.delete(path).status_code == 404
    assert client.get(f'{API}/{replacement["id"]}').json() == replacement


@pytest.mark.parametrize('operation', ['create', 'update'])
def test_simultaneous_writes_have_one_winner(client, tmp_path, operation):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.services.memory_service import create_memory
    item = create(client).json() if operation == 'update' else None
    barrier = Barrier(2)
    engine = create_engine(f'sqlite:///{tmp_path / "test_tasks.db"}', connect_args={'check_same_thread':False})
    clock = FixedClock(datetime(2026, 9, 22, tzinfo=timezone.utc))
    def write(number):
        with Session(engine) as db:
            barrier.wait(timeout=10)
            try:
                if operation == 'create':
                    create_memory(db, MemoryCreate(category='preference', key='race', value=number), clock)
                else:
                    update_memory(db, item['id'], MemoryUpdate(category='preference', key='study_time', value=number, expected_version=1), clock)
                return 'OK'
            except MemoryError as exc:
                return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, [1, 2]))
    assert sorted(outcomes) == sorted(['OK', 'MEMORY_KEY_CONFLICT' if operation == 'create' else 'MEMORY_VERSION_CONFLICT'])
    assert len(client.get(API).json()) == 1
    engine.dispose()


def test_memory_crud_does_not_change_other_domains(client, tmp_path):
    task = client.post('/api/tasks', json={'title': 'preserve me', 'due_at': '2026-08-12T04:00:00Z'}).json()
    assert client.post(f'/api/tasks/{task["id"]}/confirm').status_code == 200
    assert client.post('/api/documents', files={'file': ('example.txt', b'preserve document', 'text/plain')}).status_code == 201
    from app.models.execution import Execution
    from app.models.reminder import Notification, Reminder
    engine = create_engine(f'sqlite:///{tmp_path / "test_tasks.db"}')
    with Session(engine) as db:
        reminder = db.scalar(select(Reminder))
        db.add(Notification(reminder_id=reminder.id, task_id=task['id'], title='preserve', body='existing notification'))
        db.add(Execution(owner_id='default', execution_type='calendar.create_event', status='SUCCEEDED', payload='{}', normalized_payload_hash='0'*64, idempotency_key='existing-calendar-execution'))
        db.commit()
    engine.dispose()
    before = snapshot(tmp_path)
    item = create(client).json()
    assert snapshot(tmp_path) == before
    assert client.put(f'{API}/{item["id"]}', json={'category':'stable_fact', 'key':'time', 'value':'late', 'expected_version':1}).status_code == 200
    assert snapshot(tmp_path) == before
    assert client.delete(f'{API}/{item["id"]}').status_code == 200
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize('flow', ['chat', 'legacy_task', 'action_task', 'calendar', 'rag'])
def test_no_capture_or_context_injection(client, fake_provider, tmp_path, flow):
    marker = 'MEMORY_SENTINEL_10A_DO_NOT_INJECT'
    assert create(client, value=marker).status_code == 201
    before = snapshot(tmp_path, memory_only=True)
    cid = client.post('/api/conversations', json={'title':'boundary'}).json()['id']
    base = f'/api/conversations/{cid}'
    if flow == 'chat':
        response = client.post(base + '/messages', json={'content':'你好'})
        assert response.status_code == 201
    elif flow == 'legacy_task':
        fake_provider.reply_json = {'action':'create_task_draft','arguments':{'title':'报告','description':None,'due_at':None},'explanation':'任务草稿'}
        response = client.post(base + '/task-proposals', json={'content':'创建报告任务','timezone':'Asia/Shanghai'})
        assert response.status_code == 201, response.text
    elif flow in ('action_task', 'calendar'):
        fake_provider.reply_json = {'intent':'create_task','task_title':'完成报告','task_due_text':None,'reminder_time_text':None,'explanation':'预览'}
        content = '完成报告'
        if flow == 'calendar':
            fake_provider.reply_json = {'intent':'create_calendar_event','task_title':None,'task_due_text':None,'reminder_time_text':None,'calendar_title':'项目同步','calendar_start_text':'明天下午3点','calendar_end_text':'4点','calendar_location':'会议室 A','explanation':'预览'}
            content = '明天下午3点到4点在会议室 A 创建项目同步日历事件'
        response = client.post(base + '/action-proposals', json={'content':content,'timezone':'Asia/Shanghai'})
        assert response.status_code == 200 and response.json()['proposal']['status'] == 'READY', response.text
    else:
        doc = client.post('/api/documents', files={'file':('facts.txt', b'JARVIS uses a local database for tasks.', 'text/plain')}).json()
        assert client.post(f'/api/retrieval/index/{doc["id"]}').status_code == 200
        fake_provider.reply_json = {'answer':'JARVIS uses a local database.','cited_labels':['C1'],'sufficient_evidence':True}
        response = client.post('/api/rag/ask', json={'question':'What database does JARVIS use?'})
        assert response.status_code == 200, response.text
    assert fake_provider.calls
    assert marker not in json.dumps(fake_provider.calls)
    assert snapshot(tmp_path, memory_only=True) == before
