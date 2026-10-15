"""Phase 11A Today: section failure isolation, partial availability, and log privacy.

A database error in one section marks only that section unavailable (all fields null) and the
others are still computed. "Unavailable" is never an empty list, and an unknown defect is never
turned into "unavailable".
"""

import logging
import sqlite3
from contextlib import contextmanager

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, OperationalError

from app.services import today_service
from tests.test_phase11a_today_projection import (  # noqa: F401
    NOW,
    ZONE,
    add_execution,
    add_notification,
    add_reminder,
    add_task,
    db,
    get_today,
    today,
)

SECTIONS = ('tasks', 'reminders', 'notifications', 'executions')
# The first table each section's own queries select from.
SECTION_TABLE = {'tasks': 'FROM tasks', 'reminders': 'FROM reminders', 'notifications': 'FROM notifications',
                 'executions': 'FROM executions'}
UNAVAILABLE = {
    'tasks': {'status': 'unavailable', 'drafts': None, 'overdue': None, 'due_today': None, 'other_open': None},
    'reminders': {'status': 'unavailable', 'scheduled': None, 'delivered': None, 'attention': None},
    'notifications': {'status': 'unavailable', 'unread': None},
    'executions': {'status': 'unavailable', 'attention': None},
}
PRIVATE = ('PRIVATE_TASK_TITLE', 'PRIVATE_NOTIFICATION', 'PRIVATE_SQL', 'PRIVATE_PARAM', 'PRIVATE_ORIG', ZONE,
           'Shanghai', 'SELECT', 'FROM ', 'Traceback')


@pytest.fixture(autouse=True)
def _today_logger_enabled(monkeypatch):
    """alembic's fileConfig (run by migration tests) disables existing loggers; re-enable ours so the
    log assertions observe real records."""
    monkeypatch.setattr(today_service.logger, 'disabled', False)


@pytest.fixture()
def seeded(client, db):
    add_task(db, 'PRIVATE_TASK_TITLE draft', status='DRAFT')
    add_task(db, 'PRIVATE_TASK_TITLE today', due_at=NOW)
    add_reminder(db, 'PENDING', NOW, title='PRIVATE_TASK_TITLE reminder')
    add_notification(db, 'PRIVATE_NOTIFICATION', NOW)
    add_execution(db, 'FAILED', finished_at=NOW)
    return today(client)


@contextmanager
def failing(*sections, error=OperationalError):
    """Fail every statement that reads the given sections' own tables, at the database driver boundary."""
    markers = tuple(SECTION_TABLE[name] for name in sections)

    def hook(_conn, _cursor, statement, _parameters, _context, _many):
        flat = ' '.join(statement.split())
        if any(marker in flat.split(' WHERE ')[0].split(' JOIN ')[0] for marker in markers):
            raise error('SELECT PRIVATE_SQL', {'p': 'PRIVATE_PARAM'}, Exception('PRIVATE_ORIG'))

    event.listen(Engine, 'before_cursor_execute', hook)
    try:
        yield
    finally:
        event.remove(Engine, 'before_cursor_execute', hook)


def today_events(caplog):
    return [r.getMessage() for r in caplog.records if r.name == today_service.logger.name]


def assert_logs_private(caplog):
    text = caplog.text + ''.join(repr(r.args) + str(r.exc_info) + str(r.exc_text) for r in caplog.records
                                 if r.name == today_service.logger.name)
    own = '\n'.join(today_events(caplog))
    for canary in PRIVATE:
        assert canary not in own, canary
    for canary in ('PRIVATE_TASK_TITLE', 'PRIVATE_NOTIFICATION', 'PRIVATE_PARAM', 'PRIVATE_ORIG'):
        assert canary not in text, canary
    assert all(r.exc_info is None for r in caplog.records if r.name == today_service.logger.name)


# ------------------------------------------------------------------ one section at a time

@pytest.mark.parametrize('section', SECTIONS)
@pytest.mark.parametrize('error', [OperationalError, IntegrityError])
def test_one_failing_section_is_isolated(client, seeded, caplog, section, error):
    caplog.set_level(logging.DEBUG)
    with failing(section, error=error):
        response = get_today(client)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    body = response.json()
    assert body[section] == UNAVAILABLE[section]  # every field null: never an empty list, never zero
    for other in SECTIONS:
        if other != section:
            assert body[other] == seeded[other] and body[other]['status'] == 'ok'
    for key in ('timezone', 'local_date', 'generated_at', 'day_start', 'day_end'):
        assert body[key] == seeded[key]
    assert today_events(caplog) == [
        f'today.section_unavailable section={section} error={error.__name__}',
        'today.projection timezone_valid=true sections_ok=3 sections_unavailable=1',
    ]
    assert_logs_private(caplog)
    # Nothing is left broken: the next request is fully readable again.
    assert today(client) == seeded


def test_several_failing_sections_still_return_the_readable_ones(client, seeded, caplog):
    caplog.set_level(logging.DEBUG)
    with failing('tasks', 'notifications', 'executions'):
        response = get_today(client)
    assert response.status_code == 200
    body = response.json()
    assert [body[name]['status'] for name in SECTIONS] == ['unavailable', 'ok', 'unavailable', 'unavailable']
    assert body['reminders'] == seeded['reminders']
    assert today_events(caplog)[-1] == 'today.projection timezone_valid=true sections_ok=1 sections_unavailable=3'


def test_all_sections_failing_is_503_not_an_empty_success(client, seeded, caplog):
    caplog.set_level(logging.DEBUG)
    with failing(*SECTIONS):
        response = get_today(client)
    assert response.status_code == 503
    assert response.json() == {'error': {'code': 'SERVICE_UNAVAILABLE', 'message': 'Database is unavailable'}}
    assert response.headers['cache-control'] == 'no-store'
    assert today_events(caplog) == [f'today.section_unavailable section={name} error=OperationalError'
                                    for name in SECTIONS] + [
        'today.projection timezone_valid=true sections_ok=0 sections_unavailable=4']
    assert_logs_private(caplog)
    for canary in PRIVATE[:5]:
        assert canary not in response.text


def test_a_real_missing_table_isolates_that_section(client, seeded, tmp_path):
    with sqlite3.connect(tmp_path / 'test_tasks.db') as con:
        con.execute('DROP TABLE notifications')
    body = today(client)
    assert body['notifications'] == UNAVAILABLE['notifications']
    assert body['tasks'] == seeded['tasks'] and body['reminders'] == seeded['reminders']
    assert body['executions'] == seeded['executions']


# ------------------------------------------------------------------ empty is not unavailable

def test_readable_empty_sections_differ_from_unavailable_ones(client):
    empty = today(client)
    for name in SECTIONS:
        assert empty[name]['status'] == 'ok' and empty[name] != UNAVAILABLE[name]
    assert empty['notifications']['unread'] == {'items': [], 'total': 0, 'truncated': False}
    with failing('notifications'):
        broken = today(client)
    assert broken['notifications'] == {'status': 'unavailable', 'unread': None}
    assert broken['notifications']['unread'] is None and empty['notifications']['unread'] is not None


# ------------------------------------------------------------------ defects are not swallowed

@pytest.mark.parametrize('index', range(4))
def test_non_database_defect_in_a_section_is_a_500_not_unavailable(client, seeded, monkeypatch, caplog, index):
    caplog.set_level(logging.DEBUG)

    def defect(_db, _window):
        raise ValueError('PRIVATE_DEFECT in section code')

    sections = list(today_service._SECTIONS)
    name, section_type, _read = sections[index]
    sections[index] = (name, section_type, defect)
    monkeypatch.setattr(today_service, '_SECTIONS', tuple(sections))
    response = get_today(client)
    assert response.status_code == 500
    assert response.json() == {'error': {'code': 'INTERNAL_ERROR', 'message': 'Internal server error'}}
    assert response.headers['cache-control'] == 'no-store'
    assert 'unavailable' not in response.text and 'PRIVATE' not in response.text
    assert not any('section_unavailable' in message for message in today_events(caplog))


def test_validation_failure_logs_nothing_and_reads_nothing(client, seeded, caplog):
    caplog.set_level(logging.DEBUG)
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    event.listen(Engine, 'before_cursor_execute', record)
    try:
        response = client.get('/api/today?timezone=Not/AZone_CANARY')
    finally:
        event.remove(Engine, 'before_cursor_execute', record)
    assert response.status_code == 422
    assert statements == [] and today_events(caplog) == []
    assert 'CANARY' not in caplog.text


def test_success_logs_only_the_fixed_projection_event(client, seeded, caplog):
    caplog.set_level(logging.DEBUG)
    today(client)
    assert today_events(caplog) == ['today.projection timezone_valid=true sections_ok=4 sections_unavailable=0']
    assert_logs_private(caplog)
