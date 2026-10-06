"""Phase 10C: provider request bytes and logging privacy on the installed SDK transport family.

Offline only. The real OpenAIChatProvider / DeepSeekChatProvider and the real openai SDK run
through the full ordinary-Chat API path over native httpx2.MockTransport (the SDK's installed
transport family; not a legacy httpx substitute) and, once, over the SDK's default client
against a temporary 127.0.0.1 server so the real httpcore2 loggers are exercised.

Three different counts are kept apart on purpose:
- application generation calls (provider.generate): at most one per send;
- in-process HTTP requests seen by the transport;
- SDK transport retries (HTTP requests minus one), which this phase does not change.
"""

import json
import logging
import sqlite3

import httpx2
import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import app.services.memory_context_service as mcs
from app.llm.deepseek_provider import DeepSeekChatProvider
from app.llm.factory import get_llm_provider
from app.llm.openai_provider import OpenAIChatProvider
from app.llm.sdk_logging import REDACTED_MESSAGE, SdkLogRedactionFilter
from app.main import app
from app.models.conversation import Message
from app.services.memory_context_service import CANONICAL_KEYS, MEMORY_CONTEXT_POLICY
from tests.test_phase10b_sdk_log_privacy import (  # noqa: F401  (helpers and pytest fixtures)
    KEY_SENTINEL,
    MODEL,
    Transport,
    captured_text,
    completion,
    default_provider,
    direct_loopback_env,
    loopback,
    named,
    no_sleep,
    real_provider,
    sdk_debug,
)

API = '/api/conversations'
TIME, DURATION, DAYS = CANONICAL_KEYS
PROVIDERS = [OpenAIChatProvider, DeepSeekChatProvider]
USER_CANARY = 'USER_SENTENCE_CANARY_10C_PRIVACY'
REPLY_CANARY = 'REPLY_CANARY_10C_PRIVACY'
SECRET = 'UNSELECTED_SECRET_CANARY_10C'
ERROR_CANARY = 'UPSTREAM_ERROR_BODY_CANARY_10C'
MEMORY_INSERT = ("INSERT INTO personal_memories(category,key,value_json,source_type,version,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?)")
# Nothing below may appear in any captured log record or client-visible error.
PRIVATE = (USER_CANARY, REPLY_CANARY, SECRET, ERROR_CANARY, KEY_SENTINEL, 'KEY_SENTINEL', 'Bearer',
           'JARVIS_SAVED_PREFERENCES', 'Saved preferences below', 'preferred_study_time',
           'preferred_session_duration', 'usual_available_days', '"evening"', "'evening'", '"value"', '"messages"',
           "'messages'", 'json_data', 'home_city')


APP_LOGGERS = ('app.services.memory_context_service', 'app.services.chat_service', 'app.llm.openai_provider',
               'app.llm.messages', 'app.main')


@pytest.fixture(autouse=True)
def _app_loggers_enabled(monkeypatch):
    """alembic's fileConfig (run by migration tests) disables existing loggers; re-enable the
    application loggers so log-privacy assertions observe real records and are never vacuous."""
    for name in APP_LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), 'disabled', False)


def assert_private(text):
    for canary in PRIVATE:
        assert canary not in text, canary
    assert 'authorization' not in text.lower()


@pytest.fixture()
def memory(tmp_path):
    path = tmp_path / 'test_tasks.db'

    def seed():
        with sqlite3.connect(path) as con:
            for category, key, raw in (('preference', TIME, '"evening"'), ('preference', DURATION, '45'),
                                       ('constraint', DAYS, '["mon","wed"]'),
                                       ('stable_fact', 'home_city', f'"{SECRET}"')):
                con.execute(MEMORY_INSERT, (category, key, raw, 'manual', 1, '2026-01-01', '2026-01-01'))
        return path

    return seed


def install(provider, monkeypatch):
    """Route the API to this real provider and count application-level generation calls."""
    generations = []
    real_generate = provider.generate

    def counted(messages, *, response_schema=None):
        generations.append(len(messages))
        return real_generate(messages, response_schema=response_schema)

    monkeypatch.setattr(provider, 'generate', counted)
    app.dependency_overrides[get_llm_provider] = lambda: provider
    return generations


def provider_http_records(caplog):
    """httpx2 records for the provider call only (Starlette's TestClient is itself httpx2: 'testserver')."""
    return [r for r in named(caplog, 'httpx2') if 'testserver' not in r.getMessage()]


def send(client, keys, content=USER_CANARY):
    cid = client.post(API, json={'title': 'privacy'}).json()['id']
    return cid, client.post(f'{API}/{cid}/messages', json={'content': content, 'memory_keys': keys})


EXPECTED_MESSAGES = [
    {'role': 'system', 'content': MEMORY_CONTEXT_POLICY},
    {'role': 'user', 'content': 'JARVIS_SAVED_PREFERENCES_V1\n{"preferences":[{"key":"preferred_study_time",'
                                '"value":"evening"},{"key":"preferred_session_duration","value":45}]}\n'
                                'END_JARVIS_SAVED_PREFERENCES_V1'},
    {'role': 'user', 'content': USER_CANARY},
]


# ------------------------------------------------------------------ request bytes on the wire

@pytest.mark.parametrize('cls', PROVIDERS)
def test_sdk_serializes_exactly_the_frozen_payload(cls, client, memory, monkeypatch, sdk_debug):
    path = memory()
    transport = Transport(lambda request: httpx2.Response(200, json=completion(REPLY_CANARY)))
    generations = install(real_provider(cls, transport), monkeypatch)

    _cid, response = send(client, [DURATION, TIME])

    assert response.status_code == 201
    body = response.json()
    assert body['memory_context'] == {'status': 'applied', 'keys': [TIME, DURATION]}
    assert body['assistant_message']['content'] == REPLY_CANARY
    assert body['assistant_message']['memory_context_applied'] is True
    # One application generation call; one in-process HTTP request; zero SDK retries on success.
    assert generations == [3] and len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == 'POST' and request.url.path.endswith('/chat/completions')
    assert request.headers['x-stainless-retry-count'] == '0'
    wire = json.loads(request.content)
    assert wire == {'model': MODEL, 'messages': EXPECTED_MESSAGES}  # no tools, no response_format, nothing else
    raw = request.content.decode('utf-8')
    for forbidden in (SECRET, 'home_city', 'usual_available_days', 'mon', 'manual', 'stable_fact', 'version',
                      'created_at', 'tools', 'tool_choice', 'function'):
        assert forbidden not in raw, forbidden
    # Logs: openai.* records exist and are redacted; nothing private anywhere, including DEBUG.
    records = [r for r in sdk_debug.records if r.name.startswith('openai')]
    assert records and all(r.getMessage() == REDACTED_MESSAGE for r in records)
    assert len(provider_http_records(sdk_debug)) == 1
    assert_private(captured_text(sdk_debug))
    with sqlite3.connect(path) as con:
        assert con.execute('SELECT role, memory_context_applied FROM messages ORDER BY id').fetchall() == [
            ('USER', 0), ('ASSISTANT', 1)]


@pytest.mark.parametrize('cls', PROVIDERS)
def test_no_selection_sends_no_memory_bytes(cls, client, memory, monkeypatch):
    memory()
    transport = Transport(lambda request: httpx2.Response(200, json=completion('ok')))
    generations = install(real_provider(cls, transport), monkeypatch)
    _cid, response = send(client, [])
    assert response.status_code == 201 and generations == [1] and len(transport.requests) == 1
    assert json.loads(transport.requests[0].content) == {
        'model': MODEL, 'messages': [{'role': 'user', 'content': USER_CANARY}]}
    raw = transport.requests[0].content.decode('utf-8')
    assert 'JARVIS_SAVED_PREFERENCES' not in raw and 'Saved preferences' not in raw and 'evening' not in raw


# ------------------------------------------------------------------ retries: three separate counts

@pytest.mark.parametrize('cls', PROVIDERS)
def test_transport_retries_are_not_application_generations(cls, client, memory, monkeypatch, sdk_debug, no_sleep):
    path = memory()
    error_body = {'error': {'message': f'{ERROR_CANARY} {USER_CANARY} evening', 'type': 'server_error'}}
    transport = Transport(lambda request: httpx2.Response(500, json=error_body))
    generations = install(real_provider(cls, transport), monkeypatch)

    _cid, response = send(client, [TIME, DURATION])

    assert response.status_code == 502
    assert response.json() == {'error': {'code': 'LLM_UPSTREAM_ERROR', 'message': 'LLM provider returned an error'}}
    assert response.headers['cache-control'] == 'no-store'
    assert len(generations) == 1            # application generation calls
    assert len(transport.requests) == 3     # in-process HTTP requests
    assert [r.headers['x-stainless-retry-count'] for r in transport.requests] == ['0', '1', '2']  # 2 SDK retries
    # The SDK replays the same bytes; the application neither shrinks nor enlarges the context.
    assert {r.content for r in transport.requests} == {transport.requests[0].content}
    assert json.loads(transport.requests[0].content)['messages'] == EXPECTED_MESSAGES
    assert_private(captured_text(sdk_debug) + response.text)
    with sqlite3.connect(path) as con:  # USER-first; no fabricated or unmarked answer
        assert con.execute('SELECT role, memory_context_applied FROM messages').fetchall() == [('USER', 0)]


@pytest.mark.parametrize('failure, code', [(httpx2.ConnectError, 'LLM_TIMEOUT'), (httpx2.ReadTimeout, 'LLM_TIMEOUT')])
def test_transport_failure_is_a_safe_provider_error(failure, code, client, memory, monkeypatch, sdk_debug, no_sleep):
    memory()

    def fail(request):
        raise failure(f'{ERROR_CANARY} {USER_CANARY}', request=request)

    transport = Transport(fail)
    generations = install(real_provider(DeepSeekChatProvider, transport), monkeypatch)
    _cid, response = send(client, [TIME])
    assert response.status_code == 502 and response.json()['error']['code'] == code
    assert len(generations) == 1 and len(transport.requests) == 3
    assert_private(captured_text(sdk_debug) + response.text)


@pytest.mark.parametrize('cls', PROVIDERS)
def test_context_rejection_is_not_retried_with_a_different_context(cls, client, memory, monkeypatch, sdk_debug):
    memory()
    error_body = {'error': {'message': f'context rejected {ERROR_CANARY}', 'type': 'invalid_request_error'}}
    transport = Transport(lambda request: httpx2.Response(400, json=error_body))
    generations = install(real_provider(cls, transport), monkeypatch)
    _cid, response = send(client, list(CANONICAL_KEYS))
    assert response.status_code == 502 and response.json()['error']['code'] == 'LLM_UPSTREAM_ERROR'
    assert len(generations) == 1 and len(transport.requests) == 1  # no second, smaller or larger request
    assert_private(captured_text(sdk_debug) + response.text)


# ------------------------------------------------------------------ default client → httpx2 → httpcore2

def test_default_client_loopback_with_memory_context_logs_nothing_private(
        client, memory, monkeypatch, loopback, default_provider, sdk_debug):
    memory()
    loopback.payload = completion(REPLY_CANARY)
    provider = default_provider(DeepSeekChatProvider, loopback.base_url)
    assert type(provider._client._client).__name__ == 'SyncHttpxClientWrapper'  # the SDK's own default client
    generations = install(provider, monkeypatch)

    _cid, response = send(client, [TIME, DURATION])

    assert response.status_code == 201 and response.json()['assistant_message']['memory_context_applied'] is True
    assert generations == [3] and len(loopback.requests) == 1
    assert loopback.requests[0]['peer'] == '127.0.0.1'
    assert loopback.requests[0]['body'] == {'model': MODEL, 'messages': EXPECTED_MESSAGES}
    # Real transport DEBUG records were produced, on a direct loopback connection.
    connection = [r.getMessage() for r in named(sdk_debug, 'httpcore2.connection')]
    http11 = [r.getMessage() for r in named(sdk_debug, 'httpcore2.http11')]
    assert any(m.startswith('connect_tcp.started') and f'port={loopback.port}' in m for m in connection)
    assert any(m.startswith('send_request_body.started') for m in http11)
    assert any(m.startswith('receive_response_headers.complete') for m in http11)
    assert not named(sdk_debug, 'httpcore2.proxy')
    assert_private(captured_text(sdk_debug))


# ------------------------------------------------------------------ negative controls

def test_negative_control_detector_sees_a_leak_when_one_exists(client, memory, monkeypatch, sdk_debug):
    """SYNTHETIC control: with the SDK filter removed, a body-bearing record is detected.
    openai 3.24 does not emit such a record itself; this only proves the assertions are live."""
    sdk_logger = logging.getLogger('openai._base_client')
    installed = [f for f in sdk_logger.filters if isinstance(f, SdkLogRedactionFilter)]
    assert len(installed) == 1
    sdk_logger.debug('Request options: %s', {'json_data': {'messages': EXPECTED_MESSAGES}})
    assert_private(captured_text(sdk_debug))  # filter installed: redacted
    sdk_debug.clear()
    sdk_logger.removeFilter(installed[0])
    try:
        sdk_logger.debug('Request options: %s', {'json_data': {'messages': EXPECTED_MESSAGES}})
        with pytest.raises(AssertionError):
            assert_private(captured_text(sdk_debug))
    finally:
        sdk_logger.addFilter(installed[0])
    sdk_debug.clear()
    logging.getLogger('app.services.chat_service').warning('synthetic leak %s', USER_CANARY)
    with pytest.raises(AssertionError):
        assert_private(captured_text(sdk_debug))


# ------------------------------------------------------------------ application logs under DEBUG

def test_application_logs_stay_private_across_success_and_failure_paths(client, memory, monkeypatch, sdk_debug):
    path = memory()
    transport = Transport(lambda request: httpx2.Response(200, json=completion(REPLY_CANARY)))
    install(real_provider(OpenAIChatProvider, transport), monkeypatch)

    # 1. success with context
    cid, response = send(client, [TIME, DURATION])
    assert response.status_code == 201

    # 2. serializer failure → unavailable
    with monkeypatch.context() as patch:
        def broken(_value):
            raise ValueError(f'serializer {SECRET} evening')
        patch.setattr(mcs, '_dumps', broken)
        degraded = client.post(f'{API}/{cid}/messages', json={'content': USER_CANARY, 'memory_keys': [TIME]})
        assert degraded.json()['memory_context'] == {'status': 'unavailable', 'keys': []}

    # 3. Memory read failure → unavailable
    with monkeypatch.context() as patch:
        def unreadable(_bind, _requested):
            raise RuntimeError(f'read {SECRET} preferred_study_time')
        patch.setattr(mcs, '_read_candidates', unreadable)
        assert client.post(f'{API}/{cid}/messages', json={'content': USER_CANARY, 'memory_keys': [TIME]}
                           ).json()['memory_context']['status'] == 'unavailable'

    # 4. ASSISTANT insert failure with a parameter-bearing database error
    def hook(session, _flush_context, _instances):
        if any(isinstance(obj, Message) and obj.role == 'ASSISTANT' for obj in session.new):
            raise IntegrityError(f'INSERT {REPLY_CANARY}', {'content': REPLY_CANARY}, Exception(REPLY_CANARY))

    event.listen(Session, 'before_flush', hook)
    try:
        failed = client.post(f'{API}/{cid}/messages', json={'content': USER_CANARY, 'memory_keys': [TIME]})
    finally:
        event.remove(Session, 'before_flush', hook)
    assert failed.status_code == 500 and failed.json()['error']['code'] == 'CHAT_PERSISTENCE_FAILED'

    text = captured_text(sdk_debug)
    assert_private(text + failed.text)  # logs and the error body; a 201 body legitimately carries the transcript
    assert 'INSERT' not in text and 'Traceback' not in text
    # The only new events are fixed codes with status / count / byte fields.
    events = [r.getMessage() for r in sdk_debug.records if r.name == mcs.logger.name]
    assert events[0].startswith('chat.memory_context status=applied included=2 bytes=')
    assert events.count('chat.memory_context status=unavailable included=0 bytes=0') == 2
    assert 'chat.persistence_failed' in [r.getMessage() for r in sdk_debug.records
                                         if r.name == 'app.services.chat_service']
    assert not [r for r in sdk_debug.records if r.name.startswith('app.services') and r.exc_info]
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT COUNT(*) FROM messages WHERE role='ASSISTANT'").fetchone() == (3,)
