"""Phase 11A Today: exact section eligibility, ordering, limits and schema.

Offline: fixed clock, disposable file-backed SQLite, rows written through the ORM models as the
owning domains store them (UTC). Shared helpers in this module are reused by the other
Phase 11A test modules.
"""

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.clock import FixedClock, get_clock
from app.main import app
from app.models.execution import Execution
from app.models.execution_step import ExecutionStep
from app.models.reminder import Notification, Reminder
from app.models.task import Task

UTC = timezone.utc
ZONE = 'Asia/Shanghai'
# Fixed "now": 2026-08-11 12:00 in Asia/Shanghai.
NOW = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)
DAY_START = datetime(2026, 8, 10, 16, 0, tzinfo=UTC)   # 2026-08-11 00:00 +08:00
DAY_END = datetime(2026, 8, 11, 16, 0, tzinfo=UTC)     # 2026-08-12 00:00 +08:00
LOOKBACK_START = datetime(2026, 8, 4, 16, 0, tzinfo=UTC)  # 2026-08-05 00:00 +08:00
US = timedelta(microseconds=1)
HOUR = timedelta(hours=1)
_keys = itertools.count(1)

TOP_KEYS = {'timezone', 'local_date', 'generated_at', 'day_start', 'day_end', 'tasks', 'reminders',
            'notifications', 'executions'}
LIST_KEYS = {'items', 'total', 'truncated'}
TASK_KEYS = {'id', 'title', 'status', 'due_at'}
REMINDER_KEYS = {'id', 'task_id', 'task_title', 'status', 'remind_at'}
NOTIFICATION_KEYS = {'id', 'task_id', 'title', 'body', 'created_at'}
EXECUTION_KEYS = {'id', 'execution_type', 'status', 'reason', 'occurred_at'}


def iso(value):
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


@pytest.fixture()
def db(client, tmp_path):
    """A session on the client fixture's disposable database."""
    engine = create_engine(f"sqlite:///{(tmp_path / 'test_tasks.db').as_posix()}")
    with Session(engine) as session:
        yield session
    engine.dispose()


def set_clock(now):
    app.dependency_overrides[get_clock] = lambda: FixedClock(now)


def get_today(client, zone=ZONE):
    return client.get('/api/today', params={'timezone': zone})


def today(client, zone=ZONE):
    response = get_today(client, zone)
    assert response.status_code == 200, response.text
    return response.json()


def add_task(db, title, status='CONFIRMED', due_at=None):
    task = Task(title=title, status=status, due_at=due_at)
    db.add(task)
    db.commit()
    return task.id


def add_reminder(db, status, remind_at, title=None, **extra):
    task_id = add_task(db, title or f'reminder task {next(_keys)}')
    reminder = Reminder(task_id=task_id, status=status, remind_at=remind_at, **extra)
    db.add(reminder)
    db.commit()
    return reminder.id


def add_notification(db, title, created_at, read_at=None):
    reminder_id = add_reminder(db, 'DELIVERED', created_at)
    task_id = db.get(Reminder, reminder_id).task_id
    notification = Notification(reminder_id=reminder_id, task_id=task_id, title=title, body=f'body of {title}',
                                created_at=created_at, read_at=read_at)
    db.add(notification)
    db.commit()
    return notification.id


def add_execution(db, status, *, execution_type='documents.reindex', owner_id='default', finished_at=None,
                  updated_at=None, retry_of_id=None, confirmation=None, **extra):
    key = next(_keys)
    execution = Execution(owner_id=owner_id, execution_type=execution_type, status=status, payload='{}',
                          normalized_payload_hash='0' * 64, idempotency_key=f'key-{key}', finished_at=finished_at,
                          retry_of_id=retry_of_id, last_error_code='EXEC_SECRET_CODE',
                          last_error_message='SECRET_ERROR_MESSAGE', **extra)
    db.add(execution)
    db.commit()
    if updated_at is not None:
        db.execute(Execution.__table__.update().where(Execution.id == execution.id).values(updated_at=updated_at))
        db.commit()
    if confirmation is not None:
        requires, state = confirmation
        db.add(ExecutionStep(execution_id=execution.id, step_key='run', step_index=0, tool_name=execution_type,
                             status='FAILED', input_hash='1' * 64, input_json='{"SECRET_INPUT": 1}',
                             requires_confirmation=requires, confirmation_status=state,
                             error_message='SECRET_STEP_ERROR', created_at=NOW, updated_at=NOW))
        db.commit()
    return execution.id


def titles(task_list):
    return [item['title'] for item in task_list['items']]


# ------------------------------------------------------------------ envelope / schema

def test_empty_database_returns_readable_empty_sections_with_exact_schema(client):
    body = today(client)
    assert set(body) == TOP_KEYS
    assert body['timezone'] == ZONE and body['local_date'] == '2026-08-11'
    assert (body['generated_at'], body['day_start'], body['day_end']) == (iso(NOW), iso(DAY_START), iso(DAY_END))
    empty = {'items': [], 'total': 0, 'truncated': False}
    assert body['tasks'] == {'status': 'ok', 'drafts': empty, 'overdue': empty, 'due_today': empty,
                             'other_open': {'later': 0, 'no_due_date': 0}}
    assert body['reminders'] == {'status': 'ok', 'scheduled': empty, 'delivered': empty, 'attention': empty}
    assert body['notifications'] == {'status': 'ok', 'unread': empty}
    assert body['executions'] == {'status': 'ok', 'attention': empty}


def test_response_is_deterministic_under_a_fixed_clock(client, db):
    add_task(db, 'due', due_at=NOW)
    add_reminder(db, 'PENDING', NOW)
    add_notification(db, 'n', NOW)
    add_execution(db, 'FAILED', finished_at=NOW)
    assert get_today(client).content == get_today(client).content


def test_item_key_sets_are_exact_and_carry_no_extra_fields(client, db):
    add_task(db, 'draft', status='DRAFT')
    add_task(db, 'today', due_at=NOW)
    add_reminder(db, 'PENDING', NOW)
    add_notification(db, 'unread', NOW)
    add_execution(db, 'FAILED', finished_at=NOW)
    body = today(client)
    for name in ('drafts', 'overdue', 'due_today'):
        assert set(body['tasks'][name]) == LIST_KEYS
    assert set(body['tasks']['drafts']['items'][0]) == TASK_KEYS
    assert set(body['tasks']['due_today']['items'][0]) == TASK_KEYS
    assert set(body['reminders']['scheduled']['items'][0]) == REMINDER_KEYS
    assert set(body['notifications']['unread']['items'][0]) == NOTIFICATION_KEYS
    assert set(body['executions']['attention']['items'][0]) == EXECUTION_KEYS
    assert set(body['tasks']) == {'status', 'drafts', 'overdue', 'due_today', 'other_open'}
    assert set(body['reminders']) == {'status', 'scheduled', 'delivered', 'attention'}
    assert set(body['notifications']) == {'status', 'unread'}
    assert set(body['executions']) == {'status', 'attention'}


# ------------------------------------------------------------------ tasks

def test_task_lists_follow_the_frozen_eligibility(client, db):
    add_task(db, 'draft no due', status='DRAFT')
    add_task(db, 'draft past due', status='DRAFT', due_at=DAY_START - 5 * HOUR)
    add_task(db, 'draft due today', status='DRAFT', due_at=NOW)
    add_task(db, 'draft future', status='DRAFT', due_at=DAY_END + HOUR)
    add_task(db, 'overdue', due_at=DAY_START - HOUR)
    add_task(db, 'overdue long ago', due_at=DAY_START - timedelta(days=400))
    add_task(db, 'due today', due_at=NOW)
    add_task(db, 'later', due_at=DAY_END + HOUR)
    add_task(db, 'later far', due_at=DAY_END + timedelta(days=30))
    add_task(db, 'no due date')
    add_task(db, 'done overdue', status='DONE', due_at=DAY_START - HOUR)
    add_task(db, 'done today', status='DONE', due_at=NOW)
    add_task(db, 'done no due', status='DONE')
    tasks = today(client)['tasks']
    # A draft is only ever a draft: never overdue, never due today.
    assert titles(tasks['drafts']) == ['draft past due', 'draft due today', 'draft future', 'draft no due']
    assert titles(tasks['overdue']) == ['overdue long ago', 'overdue']
    assert titles(tasks['due_today']) == ['due today']
    assert tasks['other_open'] == {'later': 2, 'no_due_date': 1}
    assert {item['status'] for item in tasks['drafts']['items']} == {'DRAFT'}
    assert {item['status'] for name in ('overdue', 'due_today') for item in tasks[name]['items']} == {'CONFIRMED'}
    listed = [t for name in ('drafts', 'overdue', 'due_today') for t in titles(tasks[name])]
    assert len(listed) == len(set(listed)) and not any(title.startswith('done') for title in listed)


def test_task_boundaries_are_start_inclusive_and_end_exclusive(client, db):
    add_task(db, 'just before start', due_at=DAY_START - US)
    add_task(db, 'exactly at start', due_at=DAY_START)
    add_task(db, 'last instant', due_at=DAY_END - US)
    add_task(db, 'exactly at end', due_at=DAY_END)
    tasks = today(client)['tasks']
    assert titles(tasks['overdue']) == ['just before start']
    assert titles(tasks['due_today']) == ['exactly at start', 'last instant']
    assert tasks['other_open'] == {'later': 1, 'no_due_date': 0}
    assert tasks['due_today']['items'][0]['due_at'] == iso(DAY_START)  # naive-UTC storage read back as UTC


def test_task_ordering_is_due_time_then_id(client, db):
    add_task(db, 'b', due_at=NOW + HOUR)
    add_task(db, 'tie second', due_at=NOW)
    add_task(db, 'tie third', due_at=NOW)
    add_task(db, 'a', due_at=NOW - HOUR)
    assert titles(today(client)['tasks']['due_today']) == ['a', 'tie second', 'tie third', 'b']


def test_postponed_task_is_evaluated_on_its_current_due_time(client, db):
    task_id = add_task(db, 'postponed', due_at=DAY_START - HOUR)
    assert titles(today(client)['tasks']['overdue']) == ['postponed']
    response = client.patch(f'/api/tasks/{task_id}/postpone', json={'due_at': iso(NOW)})
    assert response.status_code == 200
    tasks = today(client)['tasks']
    assert titles(tasks['overdue']) == [] and titles(tasks['due_today']) == ['postponed']


@pytest.mark.parametrize('name, status, due', [('drafts', 'DRAFT', None), ('overdue', 'CONFIRMED', DAY_START - HOUR),
                                               ('due_today', 'CONFIRMED', NOW)])
def test_task_limit_total_and_truncated(client, db, name, status, due):
    for index in range(50):
        add_task(db, f't{index:02d}', status=status, due_at=due)
    section = today(client)['tasks'][name]
    assert (len(section['items']), section['total'], section['truncated']) == (50, 50, False)
    add_task(db, 't50', status=status, due_at=due)
    section = today(client)['tasks'][name]
    assert (len(section['items']), section['total'], section['truncated']) == (50, 51, True)
    assert titles(section) == [f't{index:02d}' for index in range(50)]


# ------------------------------------------------------------------ reminders

def reminder_titles(reminder_list):
    return [item['task_title'] for item in reminder_list['items']]


def test_reminder_groups_follow_the_frozen_eligibility(client, db):
    before, during, after = DAY_START - HOUR, NOW, DAY_END + HOUR
    for status in ('PENDING', 'CLAIMED', 'DELIVERED', 'CANCELLED', 'FAILED'):
        add_reminder(db, status, before, title=f'{status} before')
        add_reminder(db, status, during, title=f'{status} today')
        add_reminder(db, status, after, title=f'{status} after')
    reminders = today(client)['reminders']
    assert reminder_titles(reminders['scheduled']) == ['PENDING today', 'CLAIMED today']
    assert reminder_titles(reminders['delivered']) == ['DELIVERED today']
    assert reminder_titles(reminders['attention']) == ['PENDING before', 'CLAIMED before', 'FAILED before',
                                                       'FAILED today']
    listed = [t for name in ('scheduled', 'delivered', 'attention') for t in reminder_titles(reminders[name])]
    assert len(listed) == len(set(listed))
    assert not any('CANCELLED' in title or 'after' in title or title == 'DELIVERED before' for title in listed)
    statuses = {item['status'] for name in ('scheduled', 'delivered', 'attention') for item in reminders[name]['items']}
    assert statuses == {'PENDING', 'CLAIMED', 'DELIVERED', 'FAILED'}


def test_reminder_day_and_lookback_boundaries(client, db):
    add_reminder(db, 'PENDING', DAY_START - US, title='pending before start')
    add_reminder(db, 'PENDING', DAY_START, title='pending at start')
    add_reminder(db, 'PENDING', DAY_END - US, title='pending last instant')
    add_reminder(db, 'PENDING', DAY_END, title='pending at end')
    add_reminder(db, 'DELIVERED', DAY_START, title='delivered at start')
    add_reminder(db, 'DELIVERED', DAY_END, title='delivered at end')
    add_reminder(db, 'FAILED', LOOKBACK_START - US, title='failed before lookback')
    add_reminder(db, 'FAILED', LOOKBACK_START, title='failed at lookback')
    add_reminder(db, 'FAILED', DAY_END - US, title='failed last instant')
    add_reminder(db, 'FAILED', DAY_END, title='failed at end')
    add_reminder(db, 'PENDING', DAY_START - timedelta(days=300), title='pending long ago')
    reminders = today(client)['reminders']
    assert reminder_titles(reminders['scheduled']) == ['pending at start', 'pending last instant']
    assert reminder_titles(reminders['delivered']) == ['delivered at start']
    assert reminder_titles(reminders['attention']) == ['pending long ago', 'failed at lookback',
                                                       'pending before start', 'failed last instant']


def test_reminder_rows_carry_the_current_task_title_and_order_by_time_then_id(client, db):
    first = add_reminder(db, 'PENDING', NOW, title='same time first')
    second = add_reminder(db, 'PENDING', NOW, title='same time second')
    add_reminder(db, 'PENDING', NOW - HOUR, title='earlier')
    task_id = db.get(Reminder, first).task_id
    db.get(Task, task_id).title = 'renamed in the Task domain'
    db.commit()
    scheduled = today(client)['reminders']['scheduled']
    assert reminder_titles(scheduled) == ['earlier', 'renamed in the Task domain', 'same time second']
    assert [item['id'] for item in scheduled['items']][1:] == [first, second]
    assert scheduled['items'][1]['task_id'] == task_id and scheduled['items'][1]['remind_at'] == iso(NOW)


def test_reminder_limit_total_and_truncated(client, db):
    for _ in range(51):
        add_reminder(db, 'PENDING', NOW)
    scheduled = today(client)['reminders']['scheduled']
    assert (len(scheduled['items']), scheduled['total'], scheduled['truncated']) == (50, 51, True)


# ------------------------------------------------------------------ notifications

def test_notifications_are_unread_only_any_date_newest_first(client, db):
    add_notification(db, 'old unread', NOW - timedelta(days=400))
    add_notification(db, 'read today', NOW, read_at=NOW)
    add_notification(db, 'unread today', NOW)
    first = add_notification(db, 'tie', NOW + HOUR)
    second = add_notification(db, 'tie', NOW + HOUR)
    add_notification(db, 'read old', NOW - timedelta(days=3), read_at=NOW)
    unread = today(client)['notifications']['unread']
    assert [item['title'] for item in unread['items']] == ['tie', 'tie', 'unread today', 'old unread']
    assert [item['id'] for item in unread['items']][:2] == [second, first]  # newest first, then id descending
    assert (unread['total'], unread['truncated']) == (4, False)
    assert unread['items'][2]['body'] == 'body of unread today'


def test_notification_limit_is_twenty(client, db):
    for index in range(20):
        add_notification(db, f'n{index:02d}', NOW + timedelta(minutes=index))
    unread = today(client)['notifications']['unread']
    assert (len(unread['items']), unread['total'], unread['truncated']) == (20, 20, False)
    add_notification(db, 'n20', NOW + timedelta(minutes=20))
    unread = today(client)['notifications']['unread']
    assert (len(unread['items']), unread['total'], unread['truncated']) == (20, 21, True)
    assert [item['title'] for item in unread['items']] == [f'n{index:02d}' for index in range(20, 0, -1)]


def test_mark_read_through_the_existing_endpoint_removes_it_from_today(client, db):
    notification_id = add_notification(db, 'to read', NOW)
    assert today(client)['notifications']['unread']['total'] == 1
    assert client.post(f'/api/notifications/{notification_id}/read').status_code == 200
    assert today(client)['notifications']['unread'] == {'items': [], 'total': 0, 'truncated': False}


# ------------------------------------------------------------------ executions

def reasons(execution_list):
    return [(item['id'], item['reason']) for item in execution_list['items']]


def test_execution_attention_follows_the_frozen_rules(client, db):
    pending = add_execution(db, 'RUNNING', confirmation=(True, 'PENDING'), updated_at=NOW)
    expired = add_execution(db, 'QUEUED', confirmation=(True, 'EXPIRED'), updated_at=NOW - HOUR)
    cancel_requested = add_execution(db, 'CANCEL_REQUESTED', confirmation=(True, 'PENDING'), updated_at=NOW - 2 * HOUR)
    failed = add_execution(db, 'FAILED', finished_at=NOW)
    timed_out = add_execution(db, 'TIMED_OUT', finished_at=NOW)
    # Excluded:
    add_execution(db, 'RUNNING', confirmation=(True, 'GRANTED'))
    add_execution(db, 'RUNNING', confirmation=(True, 'REJECTED'))
    add_execution(db, 'RUNNING', confirmation=(False, None))
    add_execution(db, 'QUEUED')
    add_execution(db, 'CREATED')
    add_execution(db, 'CLAIMED')
    add_execution(db, 'SUCCEEDED', finished_at=NOW)
    add_execution(db, 'SUCCEEDED', finished_at=NOW, confirmation=(True, 'PENDING'))  # terminal: no longer waiting
    add_execution(db, 'CANCELLED', finished_at=NOW, confirmation=(True, 'PENDING'))
    add_execution(db, 'FAILED', finished_at=NOW, execution_type='action.confirm')    # internal type
    add_execution(db, 'RUNNING', confirmation=(True, 'PENDING'), execution_type='action.confirm')
    add_execution(db, 'FAILED', finished_at=NOW, owner_id='someone-else')
    add_execution(db, 'RUNNING', confirmation=(True, 'PENDING'), owner_id='someone-else')
    attention = today(client)['executions']['attention']
    assert reasons(attention) == [(pending, 'confirmation_required'), (expired, 'confirmation_required'),
                                  (cancel_requested, 'confirmation_required'), (failed, 'failed'),
                                  (timed_out, 'timed_out')]
    assert (attention['total'], attention['truncated']) == (5, False)
    assert [item['status'] for item in attention['items']] == ['RUNNING', 'QUEUED', 'CANCEL_REQUESTED', 'FAILED',
                                                               'TIMED_OUT']
    assert attention['items'][3]['occurred_at'] == iso(NOW)


def test_superseded_failures_and_lookback_edges(client, db):
    retried = add_execution(db, 'FAILED', finished_at=NOW)
    add_execution(db, 'SUCCEEDED', finished_at=NOW, retry_of_id=retried)       # retried: no longer needs attention
    retried_timeout = add_execution(db, 'TIMED_OUT', finished_at=NOW)
    retry_failed = add_execution(db, 'FAILED', finished_at=NOW + HOUR, retry_of_id=retried_timeout)
    at_lookback = add_execution(db, 'FAILED', finished_at=LOOKBACK_START)
    add_execution(db, 'FAILED', finished_at=LOOKBACK_START - US)
    last_instant = add_execution(db, 'TIMED_OUT', finished_at=DAY_END - US)
    add_execution(db, 'TIMED_OUT', finished_at=DAY_END)
    no_finish = add_execution(db, 'FAILED', finished_at=None, updated_at=NOW - HOUR)  # falls back to updated_at
    add_execution(db, 'FAILED', finished_at=None, updated_at=LOOKBACK_START - HOUR)
    attention = today(client)['executions']['attention']
    assert reasons(attention) == [(retry_failed, 'failed'), (no_finish, 'failed'), (at_lookback, 'failed'),
                                  (last_instant, 'timed_out')]
    assert attention['items'][1]['occurred_at'] == iso(NOW - HOUR)


def test_execution_limit_orders_confirmation_first_and_counts_everything(client, db):
    for index in range(15):
        add_execution(db, 'FAILED', finished_at=NOW - timedelta(minutes=index))
    waiting = [add_execution(db, 'RUNNING', confirmation=(True, 'PENDING'), updated_at=NOW - timedelta(minutes=index))
               for index in range(8)]
    attention = today(client)['executions']['attention']
    assert (len(attention['items']), attention['total'], attention['truncated']) == (20, 23, True)
    assert [item['id'] for item in attention['items'][:8]] == waiting
    assert [item['reason'] for item in attention['items']] == ['confirmation_required'] * 8 + ['failed'] * 12


def test_execution_rows_expose_no_engineering_detail(client, db):
    add_execution(db, 'FAILED', finished_at=NOW)
    add_execution(db, 'RUNNING', confirmation=(True, 'PENDING'), updated_at=NOW)
    response = get_today(client)
    for forbidden in ('SECRET', 'EXEC_', 'payload', 'hash', 'idempotency', 'attempt', 'lease', 'error', 'step',
                      'owner', 'correlation', 'retry_of', 'key-'):
        assert forbidden not in response.text, forbidden
