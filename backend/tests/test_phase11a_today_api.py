"""Phase 11A Today: API contract, fixed safe validation, no-store, zero writes, zero external access.

Offline only. The zero-write proof runs on a disposable database migrated to the current head by
the normal Alembic path (the same schema as the real database), never on the real database.
"""

import hashlib
import socket
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

import app.api.today as today_api
import app.integrations.calendar.factory as calendar_factory
import app.llm.factory as llm_factory
import app.services.memory_context_service as memory_context_service
import app.services.task_service as task_service
from app.core.clock import FixedClock, get_clock
from app.core.database import get_db
from app.integrations.calendar.microsoft import MicrosoftGraphCalendarProvider
from app.llm.factory import get_llm_provider
from app.main import app
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

INVALID = {'error': {'code': 'VALIDATION_ERROR', 'message': 'Invalid Today request'}}
BACKEND = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ validation

@pytest.mark.parametrize('zone', ['UTC', 'Asia/Shanghai', 'America/New_York', 'Europe/London', 'Asia/Kathmandu',
                                  'Australia/Lord_Howe', 'Pacific/Kiritimati', 'Pacific/Pago_Pago'])
def test_valid_zones_are_accepted_and_reflected(client, zone):
    response = get_today(client, zone)
    assert response.status_code == 200 and response.json()['timezone'] == zone
    assert response.headers['cache-control'] == 'no-store'


INVALID_QUERIES = [
    '',
    'timezone=',
    'timezone=%20',
    'timezone=Mars/CANARY_ZONE',
    'timezone=%2B08:00',
    'timezone=UTC%2B8',
    'timezone=asia/shanghai',
    'timezone=Asia/Shanghai%20',
    'timezone=../../etc/CANARY_ZONE',
    'timezone=Asia/../UTC',
    'timezone=' + 'A' * 65,
    'timezone=Asia/Shanghai&timezone=Asia/Shanghai',
    'timezone=Asia/Shanghai&timezone=UTC',
    'timezone=Asia/Shanghai&date=2026-01-01',
    'timezone=Asia/Shanghai&CANARY_PARAM=1',
    'CANARY_PARAM=Asia/Shanghai',
    'Timezone=Asia/Shanghai',
    'timezone[]=Asia/Shanghai',
    'tz=Asia/Shanghai',
    'timezone=Asia/Shanghai&',
    'timezone=Asia/Shanghai&limit=500',
]


@pytest.mark.parametrize('query', INVALID_QUERIES, ids=range(len(INVALID_QUERIES)))
def test_invalid_requests_get_the_fixed_safe_422(client, db, query):
    add_task(db, 'PRIVATE_TASK_TITLE', due_at=NOW)
    response = client.get('/api/today' + ('?' + query if query else ''))
    if query == 'timezone=Asia/Shanghai&':  # a bare trailing separator carries no extra parameter
        assert response.status_code == 200
        return
    assert response.status_code == 422 and response.json() == INVALID
    assert response.headers['cache-control'] == 'no-store'
    for canary in ('CANARY', 'Mars', 'etc', 'asia', 'Shanghai', 'date', 'limit', 'loc', 'input', 'PRIVATE', 'tz'):
        assert canary not in response.text, canary


def test_unrelated_validation_format_is_unchanged(client):
    response = client.post('/api/tasks', json={'title': 'x', 'OTHER_RESOURCE_FIELD': 1})
    assert response.status_code == 422
    assert 'OTHER_RESOURCE_FIELD' in response.json()['error']['message']  # the global formatter still applies there
    assert response.headers.get('cache-control') != 'no-store'


def test_exactly_one_new_public_route_and_only_get(client):
    paths = app.openapi()['paths']
    today_paths = {path: sorted(ops) for path, ops in paths.items() if 'today' in path}
    assert today_paths == {'/api/today': ['get']}
    for method in (client.post, client.put, client.patch, client.delete):
        assert method('/api/today?timezone=UTC').status_code == 405
    assert client.get('/api/today/extra?timezone=UTC').status_code == 404


def test_request_body_is_never_read(client, db):
    add_task(db, 'due', due_at=NOW)
    plain = get_today(client).json()
    with_body = client.request('GET', '/api/today', params={'timezone': ZONE},
                               json={'timezone': 'UTC', 'now': '2000-01-01T00:00:00Z'})
    assert with_body.status_code == 200 and with_body.json() == plain


# ------------------------------------------------------------------ no-store on every Today response

def test_no_store_on_503_when_every_section_is_unavailable(client, monkeypatch):
    def unreadable(_db, _window):
        raise OperationalError('SELECT PRIVATE_SQL', {'p': 'PRIVATE_PARAM'}, Exception('PRIVATE_ORIG'))

    monkeypatch.setattr(today_service, '_SECTIONS', tuple(
        (name, section_type, unreadable) for name, section_type, _read in today_service._SECTIONS))
    response = get_today(client)
    assert response.status_code == 503
    assert response.json() == {'error': {'code': 'SERVICE_UNAVAILABLE', 'message': 'Database is unavailable'}}
    assert response.headers['cache-control'] == 'no-store' and 'PRIVATE' not in response.text


def test_no_store_on_unexpected_500_and_on_the_existing_503_handler(client, monkeypatch):
    def defect(*_args, **_kwargs):
        raise RuntimeError('PRIVATE_DEFECT_DETAIL')

    monkeypatch.setattr(today_service, 'compute_day_window', defect)
    response = get_today(client)
    assert response.status_code == 500
    assert response.json() == {'error': {'code': 'INTERNAL_ERROR', 'message': 'Internal server error'}}
    assert response.headers['cache-control'] == 'no-store' and response.headers.get('x-request-id')
    assert 'PRIVATE' not in response.text

    def unavailable(*_args, **_kwargs):
        raise OperationalError('SELECT 1', {}, Exception('PRIVATE_ORIG'))

    monkeypatch.setattr(today_service, 'compute_day_window', unavailable)  # a database error outside section isolation
    response = get_today(client)
    assert response.status_code == 503
    assert response.json() == {'error': {'code': 'SERVICE_UNAVAILABLE', 'message': 'Database is unavailable'}}
    assert response.headers['cache-control'] == 'no-store'


def test_ruling_r1_does_not_broaden_cache_behaviour(client, monkeypatch):
    assert client.get('/api/tasks').headers.get('cache-control') != 'no-store'
    assert client.get('/health').headers.get('cache-control') != 'no-store'

    def defect(*_args, **_kwargs):
        raise RuntimeError('unrelated defect')

    monkeypatch.setattr(task_service, 'list_tasks', defect)
    response = client.get('/api/tasks')
    assert response.status_code == 500 and response.headers.get('cache-control') != 'no-store'
    assert response.json() == {'error': {'code': 'INTERNAL_ERROR', 'message': 'Internal server error'}}

    def unavailable(*_args, **_kwargs):
        raise OperationalError('SELECT 1', {}, Exception('orig'))

    monkeypatch.setattr(task_service, 'list_tasks', unavailable)
    response = client.get('/api/tasks')
    assert response.status_code == 503 and response.headers.get('cache-control') != 'no-store'
    assert response.json() == {'error': {'code': 'SERVICE_UNAVAILABLE', 'message': 'Database is unavailable'}}
    # Phase 10C behaviour on the chat messages resource is untouched.
    cid = client.post('/api/conversations', json={'title': 'c'}).json()['id']
    assert client.get(f'/api/conversations/{cid}/messages').headers['cache-control'] == 'no-store'
    assert today_api.TODAY_PATH == '/api/today'


# ------------------------------------------------------------------ zero writes

@contextmanager
def captured_statements():
    seen = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        seen.append(statement.lstrip().split(None, 1)[0].upper())

    event.listen(Engine, 'before_cursor_execute', record)
    try:
        yield seen
    finally:
        event.remove(Engine, 'before_cursor_execute', record)


def dump(path):
    with sqlite3.connect(path) as con:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {table: con.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall() for table in tables}


def seed_everything(session):
    add_task(session, 'draft', status='DRAFT')
    add_task(session, 'overdue', due_at=NOW.replace(year=2025))
    add_task(session, 'today', due_at=NOW)
    for status in ('PENDING', 'CLAIMED', 'DELIVERED', 'FAILED', 'CANCELLED'):
        add_reminder(session, status, NOW, attempt_count=2, lease_expires_at=NOW)
    add_notification(session, 'unread', NOW)
    add_execution(session, 'FAILED', finished_at=NOW)
    add_execution(session, 'RUNNING', confirmation=(True, 'PENDING'), updated_at=NOW)


def test_get_today_performs_no_write_on_a_migrated_database(tmp_path):
    path = tmp_path / 'migrated.db'
    config = Config(str(BACKEND / 'alembic.ini'))
    config.set_main_option('script_location', str(BACKEND / 'alembic'))
    config.set_main_option('sqlalchemy.url', f'sqlite:///{path.as_posix()}')
    command.upgrade(config, 'head')  # the real schema, by the normal migration path
    engine = create_engine(f'sqlite:///{path.as_posix()}', connect_args={'check_same_thread': False})
    with Session(engine) as session:
        seed_everything(session)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    def override():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override
    app.dependency_overrides[get_clock] = lambda: FixedClock(NOW)
    try:
        with TestClient(app, raise_server_exceptions=False) as test_client:
            engine.dispose()
            before_bytes = hashlib.sha256(path.read_bytes()).hexdigest()
            before_rows = dump(path)
            with captured_statements() as statements:
                for zone in ('Asia/Shanghai', 'America/New_York', 'UTC'):
                    response = get_today(test_client, zone)
                    assert response.status_code == 200
                assert test_client.get('/api/today?timezone=Nope/Zone').status_code == 422
            engine.dispose()
            body = today(test_client)
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
    assert statements and set(statements) <= {'SELECT', 'PRAGMA'}, set(statements)  # no INSERT/UPDATE/DELETE/DDL
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before_bytes  # byte-identical database file
    assert dump(path) == before_rows  # every table, including sqlite_sequence and alembic_version
    assert sorted(p.name for p in tmp_path.glob('migrated.db*')) == ['migrated.db']  # no journal / WAL left behind
    assert body['tasks']['drafts']['total'] == 1 and body['reminders']['scheduled']['total'] == 2
    assert body['notifications']['unread']['total'] == 1 and body['executions']['attention']['total'] == 2


def test_reminder_rows_are_untouched_by_the_projection(client, db, tmp_path):
    for status in ('PENDING', 'CLAIMED', 'FAILED', 'DELIVERED'):
        add_reminder(db, status, NOW, attempt_count=3, lease_expires_at=NOW, last_error_code='E')
    path = tmp_path / 'test_tasks.db'
    before = dump(path)['reminders']
    for _ in range(3):
        today(client)
    assert dump(path)['reminders'] == before


# ------------------------------------------------------------------ zero external access

def test_today_touches_no_calendar_provider_memory_or_network(client, db, monkeypatch):
    seed_everything(db)
    touched = []

    def forbidden(name):
        def raiser(*_args, **_kwargs):
            touched.append(name)
            raise AssertionError(f'{name} must not be used by Today')
        return raiser

    monkeypatch.setattr(calendar_factory, 'get_calendar_provider', forbidden('calendar provider'))
    monkeypatch.setattr(calendar_factory, 'get_calendar_auth', forbidden('calendar auth'))
    for method in ('list_events', 'get_default_calendar', 'create_event', 'get_connection_status'):
        monkeypatch.setattr(MicrosoftGraphCalendarProvider, method, forbidden(f'graph {method}'))
    monkeypatch.setattr(llm_factory, 'get_llm_provider', forbidden('llm factory'))
    app.dependency_overrides[get_llm_provider] = forbidden('llm dependency')
    monkeypatch.setattr(memory_context_service, 'select_memory_context', forbidden('memory selector'))
    connections = []
    real_connect = socket.socket.connect

    def guarded(self, address):
        connections.append(address)
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    body = today(client)
    assert body['tasks']['due_today']['total'] == 1
    assert touched == [] and connections == []
    assert 'calendar' not in str(body).lower() and 'event' not in body  # no Calendar data in the projection


def test_today_modules_do_not_import_other_domains():
    """The Today modules reach no Calendar, LLM, Memory, Chat or RAG module directly."""
    for module in (today_service, today_api):
        imports = [line for line in Path(module.__file__).read_text(encoding='utf-8').splitlines()
                   if line.startswith(('import ', 'from '))]
        text = '\n'.join(imports)
        for forbidden in ('calendar', 'llm', 'memory', 'chat', 'rag', 'retrieval', 'embedding', 'execution_tools',
                          'httpx', 'requests', 'socket', 'openai'):
            assert forbidden not in text, (module.__name__, forbidden)
    source = Path(today_service.__file__).read_text(encoding='utf-8')
    for write in ('.add(', '.commit(', '.flush(', '.delete(', '.merge(', 'insert(', 'update('):
        assert write not in source, write
