"""Phase 10B Astra R2: SDK / transport DEBUG logging must not emit protected content.

Validated runtime: openai 3.24.0 -> httpx2 2.13.1 -> httpcore2 2.13.1 (the SDK's
default HTTP stack). Offline only, synthetic keys and synthetic content only:

- native ``httpx2.MockTransport`` under the real OpenAIChatProvider /
  DeepSeekChatProvider and the real SDK client (non-resolvable .invalid host);
- the SDK's normal default client against a temporary server bound to 127.0.0.1,
  which is the only path that reaches the real httpcore2 transport loggers.

No Fake Provider is used for the privacy proof. Legacy ``httpx`` is covered by one
separate compatibility test and is not the main proof.

openai 3.24 itself logs metadata only on ``openai.*`` (method, status, request id,
exception type name). ``app/llm/sdk_logging.py`` is therefore defence in depth, and
its control below injects a SYNTHETIC body-bearing record: this module does not
claim that openai 3.24 naturally emits one.
"""

import json
import logging
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import httpx2
import openai
import pytest

from app.embeddings.base import EmbeddingError
from app.embeddings.openai_provider import OpenAIEmbeddingProvider
from app.llm.base import LLMTimeoutError, LLMUpstreamError
from app.llm.deepseek_provider import DeepSeekChatProvider, build_json_mode_system_message
from app.llm.factory import get_llm_provider
from app.llm.memory_prompts import MEMORY_CANDIDATE_SYSTEM_PROMPT, build_memory_candidate_messages
from app.llm.openai_provider import OpenAIChatProvider, prepare_strict_schema
from app.llm.sdk_logging import REDACTED_MESSAGE, SdkLogRedactionFilter, protect_sdk_loggers
from app.main import app
from app.services.memory_proposal_service import MemoryCandidate
from tests.test_phase10b import PROPOSALS, cand, rows
from tests.test_phase10b import mono, store  # noqa: F401  (pytest fixtures)

BASE_URL = 'https://sdk-privacy.invalid/v1'
KEY_SENTINEL = 'sk-KEY_SENTINEL_10B_R2'
USER_SENTINEL = 'USER_SENTENCE_SENTINEL_10B_R2 I usually study on the moon'
RAW_SENTINEL = 'RAW_RESPONSE_SENTINEL_10B_R2'
BODY_SENTINEL = 'REQUEST_BODY_SENTINEL_10B_R2'  # travels only in the request JSON (model field)
MODEL = f'mock-model-{BODY_SENTINEL}'
PROMPT_SENTINEL = 'convert ONE user sentence'  # distinctive text of the Memory system prompt
# Accepted Phase 10B residual: httpcore2 logs response headers at DEBUG. This marker is
# NOT protected content; it only proves which records carry response headers.
HEADER_RESIDUAL_MARKER = 'req-HEADER_RESIDUAL_MARKER_10B'

# Loggers the installed stack really uses (see the SDK / httpx2 / httpcore2 sources).
OPENAI_LOGGERS = ('openai', 'openai._base_client', 'openai._response', 'openai._legacy_response')
TRANSPORT_LOGGERS = ('httpx2', 'httpcore2', 'httpcore2.connection', 'httpcore2.http11', 'httpcore2.proxy')
LEGACY_LOGGERS = ('httpx', 'httpcore')
PROVIDERS = [OpenAIChatProvider, DeepSeekChatProvider]
PROXY_ENV = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')


def completion(content, *, completion_id=f'chatcmpl-{RAW_SENTINEL}'):
    return {
        'id': completion_id, 'object': 'chat.completion', 'created': 1, 'model': 'mock-model',
        'system_fingerprint': RAW_SENTINEL,
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': content}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 4, 'total_tokens': 7},
    }


def error_payload(status):
    return {'error': {'message': f'status {status} {RAW_SENTINEL} {USER_SENTINEL}', 'type': 'invalid_request_error'}}


class Transport:
    """Records every request the SDK sends; answers with a scripted response."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx2.Request] = []

    def __call__(self, request):
        request.read()
        self.requests.append(request)
        return self.respond(request)


def real_provider(cls, transport):
    provider = cls(api_key=KEY_SENTINEL, model=MODEL, base_url=BASE_URL, timeout_seconds=5)
    # Same SDK client (key, base URL, timeout, default retry policy); only the transport is mocked.
    provider._client = provider._client.with_options(
        http_client=httpx2.Client(transport=httpx2.MockTransport(transport))
    )
    return provider


@pytest.fixture()
def sdk_debug(caplog, monkeypatch):
    for name in OPENAI_LOGGERS + TRANSPORT_LOGGERS + LEGACY_LOGGERS:
        # alembic's fileConfig (run by migration tests) disables existing loggers;
        # re-enable them so the stack really emits and the proof is not vacuous.
        monkeypatch.setattr(logging.getLogger(name), 'disabled', False)
        caplog.set_level(logging.DEBUG, logger=name)
    caplog.set_level(logging.DEBUG)
    return caplog


@pytest.fixture()
def no_sleep(monkeypatch):
    # Retry policy untouched; only the SDK's back-off wait is skipped for speed.
    monkeypatch.setattr(openai._base_client.time, 'sleep', lambda _seconds: None)


class Loopback:
    """Temporary HTTP server on 127.0.0.1 (ephemeral port) recording what the SDK really sent."""

    def __init__(self):
        self.requests: list[dict] = []
        self.status = 200
        self.payload = completion(f'{{"note": "{RAW_SENTINEL}"}}')
        loopback = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('content-length', 0)))
                loopback.requests.append({'path': self.path, 'body': json.loads(body),
                                          'headers': {k.lower(): v for k, v in self.headers.items()},
                                          'peer': self.client_address[0]})
                data = json.dumps(loopback.payload).encode()
                self.send_response(loopback.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('x-request-id', HEADER_RESIDUAL_MARKER)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args):  # keep the server itself silent
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.base_url = f'http://127.0.0.1:{self.port}/v1'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()


@pytest.fixture()
def direct_loopback_env(monkeypatch):
    """The default client honours proxy env vars (trust_env): make loopback strictly direct."""
    for name in PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.delenv('no_proxy', raising=False)


@pytest.fixture()
def default_provider(direct_loopback_env):
    """Builds providers that keep the SDK's own default httpx2 client; closes them afterwards."""
    built = []

    def build(cls, base_url):
        provider = cls(api_key=KEY_SENTINEL, model=MODEL, base_url=base_url, timeout_seconds=5)
        built.append(provider)
        return provider

    yield build
    for provider in built:
        provider._client.close()


@pytest.fixture()
def loopback(default_provider):  # depends on default_provider so clients close before the server
    server = Loopback()
    yield server
    server.close()


def captured_text(caplog):
    """Everything a handler could render: formatted output plus every raw record field."""
    parts = [caplog.text]
    for record in caplog.records:
        parts += [record.getMessage(), repr(record.msg), repr(record.args),
                  str(record.exc_info), str(record.exc_text), str(record.stack_info)]
    return '\n'.join(parts)


def assert_clean(text):
    for sentinel in (USER_SENTINEL, 'USER_SENTENCE_SENTINEL', PROMPT_SENTINEL, BODY_SENTINEL, RAW_SENTINEL,
                     KEY_SENTINEL, 'KEY_SENTINEL', 'Bearer', 'json_data', '"messages"', "'messages'",
                     'response_format'):
        assert sentinel not in text, sentinel
    assert 'authorization' not in text.lower()


def named(caplog, *names):
    return [r for r in caplog.records if r.name in names]


def openai_records(caplog):
    return [r for r in caplog.records if r.name == 'openai' or r.name.startswith('openai.')]


def assert_openai_records_redacted(caplog):
    records = openai_records(caplog)
    assert any(r.name == 'openai._base_client' and r.levelno == logging.DEBUG for r in records)
    for r in records:
        assert r.getMessage() == REDACTED_MESSAGE and r.args == ()
        assert r.exc_info is None and r.exc_text is None and r.stack_info is None


def messages():
    return [{'role': 'system', 'content': MEMORY_CANDIDATE_SYSTEM_PROMPT},
            {'role': 'user', 'content': USER_SENTINEL}]


def expected_body(cls):
    """The exact wire JSON each provider must send for messages() + MemoryCandidate."""
    if cls is DeepSeekChatProvider:
        return {'model': MODEL, 'messages': [build_json_mode_system_message(MemoryCandidate), *messages()],
                'response_format': {'type': 'json_object'}}
    return {'model': MODEL, 'messages': messages(),
            'response_format': {'type': 'json_schema', 'json_schema': prepare_strict_schema(MemoryCandidate)}}


def sent_body(request):
    return json.loads(request.content)


# ------------------------------------------------------------- the stack under test

def test_default_sdk_client_is_native_httpx2_not_legacy_httpx():
    provider = OpenAIChatProvider(api_key=KEY_SENTINEL, model=MODEL, base_url=BASE_URL)
    try:
        inner = provider._client._client
        assert isinstance(inner, httpx2.Client) and not isinstance(inner, httpx.Client)
        assert openai._base_client.httpx2 is httpx2
        assert provider._client.max_retries == openai.DEFAULT_MAX_RETRIES == 2
    finally:
        provider._client.close()


# ----------------------------------------------- native httpx2.MockTransport, success

@pytest.mark.parametrize('cls', PROVIDERS)
def test_success_path_debug_logs_carry_no_request_or_response_content(cls, sdk_debug):
    transport = Transport(lambda request: httpx2.Response(200, json=completion(f'{{"note": "{RAW_SENTINEL}"}}')))
    provider = real_provider(cls, transport)

    result = provider.generate(messages(), response_schema=MemoryCandidate)

    # Behaviour unchanged: the provider still sent the intended request and parsed the reply.
    assert result.text == f'{{"note": "{RAW_SENTINEL}"}}'
    assert (result.model, result.prompt_tokens, result.completion_tokens) == ('mock-model', 3, 4)
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.method == 'POST' and str(request.url) == f'{BASE_URL}/chat/completions'
    assert request.headers['authorization'] == f'Bearer {KEY_SENTINEL}'
    assert request.headers['x-stainless-retry-count'] == '0'
    assert sent_body(request) == expected_body(cls)

    # The stack really logged (the path is exercised) — openai.* only as redacted records.
    assert_openai_records_redacted(sdk_debug)
    assert [r.levelno for r in named(sdk_debug, 'httpx2')] == [logging.INFO]
    assert not named(sdk_debug, *LEGACY_LOGGERS)  # the proof does not run on legacy httpx
    assert_clean(captured_text(sdk_debug))


# ------------------------------------------------ native httpx2.MockTransport, errors

@pytest.mark.parametrize('cls', PROVIDERS)
def test_http_error_path_debug_logs_carry_no_content(cls, sdk_debug):
    transport = Transport(lambda request: httpx2.Response(400, json=error_payload(400)))
    provider = real_provider(cls, transport)

    with pytest.raises(LLMUpstreamError) as info:
        provider.generate(messages(), response_schema=MemoryCandidate)

    assert str(info.value) == 'LLM provider returned an error'
    assert type(info.value.__cause__) is openai.BadRequestError
    assert len(transport.requests) == 1  # 400 is not retried by the SDK default policy
    assert sent_body(transport.requests[0]) == expected_body(cls)
    assert_openai_records_redacted(sdk_debug)
    assert len(named(sdk_debug, 'httpx2')) == 1
    assert_clean(captured_text(sdk_debug))


@pytest.mark.parametrize('cls', PROVIDERS)
def test_retryable_http_error_keeps_sdk_retry_policy_and_request_body(cls, sdk_debug, no_sleep):
    transport = Transport(lambda request: httpx2.Response(500, json=error_payload(500)))
    provider = real_provider(cls, transport)

    with pytest.raises(LLMUpstreamError) as info:
        provider.generate(messages(), response_schema=MemoryCandidate)

    assert type(info.value.__cause__) is openai.InternalServerError
    # SDK default retry policy still applies (1 attempt + 2 retries); every attempt sends the same body.
    assert [r.headers['x-stainless-retry-count'] for r in transport.requests] == ['0', '1', '2']
    assert all(sent_body(r) == expected_body(cls) for r in transport.requests)
    assert sum(r.levelno == logging.INFO for r in openai_records(sdk_debug)) == 2  # "Retrying request ..."
    assert_openai_records_redacted(sdk_debug)
    assert len(named(sdk_debug, 'httpx2')) == 3
    assert_clean(captured_text(sdk_debug))


@pytest.mark.parametrize('failure', [httpx2.ConnectError, httpx2.ReadTimeout])
@pytest.mark.parametrize('cls', PROVIDERS)
def test_transport_failure_path_with_sdk_retries_carries_no_content(cls, failure, sdk_debug, no_sleep):
    def fail(request):
        raise failure(f'transport failed {RAW_SENTINEL} {USER_SENTINEL}', request=request)

    transport = Transport(fail)
    provider = real_provider(cls, transport)

    with pytest.raises(LLMTimeoutError) as info:
        provider.generate(messages(), response_schema=MemoryCandidate)

    expected_cause = openai.APITimeoutError if failure is httpx2.ReadTimeout else openai.APIConnectionError
    assert type(info.value.__cause__) is expected_cause
    assert RAW_SENTINEL not in str(info.value) and USER_SENTINEL not in str(info.value)
    assert len(transport.requests) == 3  # 1 attempt + 2 retries, unchanged
    assert all(sent_body(r) == expected_body(cls) for r in transport.requests)
    assert sum(r.levelno == logging.INFO for r in openai_records(sdk_debug)) == 2
    assert_openai_records_redacted(sdk_debug)
    assert_clean(captured_text(sdk_debug))


def test_openai_response_logger_path_is_redacted(sdk_debug):
    """openai._response logs when a 200 body under a non-JSON content type cannot be parsed."""
    transport = Transport(lambda request: httpx2.Response(
        200, content=f'not json {RAW_SENTINEL}'.encode(), headers={'content-type': 'text/plain'}))
    provider = real_provider(OpenAIChatProvider, transport)

    # The provider's own SDK client, called directly: this content type is outside the provider contract.
    provider._client.chat.completions.create(model=MODEL, messages=messages())

    records = named(sdk_debug, 'openai._response')
    assert records and all(r.getMessage() == REDACTED_MESSAGE for r in records)
    assert_clean(captured_text(sdk_debug))


# ------------------------------------- default client: openai -> httpx2 -> httpcore2

def assert_real_httpcore2_logging(caplog, port):
    """Real transport DEBUG records were produced, over a direct connection to the loopback port."""
    connection = [r.getMessage() for r in named(caplog, 'httpcore2.connection')]
    http11 = [r.getMessage() for r in named(caplog, 'httpcore2.http11')]
    assert all(r.levelno == logging.DEBUG for r in named(caplog, 'httpcore2.connection', 'httpcore2.http11'))
    assert any(m.startswith('connect_tcp.started') and "host='127.0.0.1'" in m and f'port={port}' in m
               for m in connection)
    assert not named(caplog, 'httpcore2.proxy', 'httpcore2.socks')  # no proxy forwarding
    return connection, http11


@pytest.mark.parametrize('cls', PROVIDERS)
def test_default_client_loopback_success_logs_no_protected_content(cls, loopback, default_provider, sdk_debug):
    provider = default_provider(cls, loopback.base_url)
    assert type(provider._client._client).__name__ == 'SyncHttpxClientWrapper'  # SDK default, not injected

    result = provider.generate(messages(), response_schema=MemoryCandidate)

    assert result.text == f'{{"note": "{RAW_SENTINEL}"}}' and result.prompt_tokens == 3
    assert len(loopback.requests) == 1
    seen = loopback.requests[0]
    assert seen['peer'] == '127.0.0.1' and seen['path'] == '/v1/chat/completions'
    assert seen['headers']['authorization'] == f'Bearer {KEY_SENTINEL}'
    assert seen['headers']['host'] == f'127.0.0.1:{loopback.port}'
    assert seen['body'] == expected_body(cls)

    _connection, http11 = assert_real_httpcore2_logging(sdk_debug, loopback.port)
    for event in ('send_request_headers.started', 'send_request_body.started',
                  'receive_response_headers.complete', 'receive_response_body.complete'):
        assert any(m.startswith(event) for m in http11), event
    assert len(named(sdk_debug, 'httpx2')) == 1
    assert_openai_records_redacted(sdk_debug)
    # Accepted residual, stated exactly: httpcore2 logs the response headers (here a request id) ...
    headers_logged = [m for m in http11 if m.startswith('receive_response_headers.complete')]
    assert headers_logged and all(HEADER_RESIDUAL_MARKER in m for m in headers_logged)
    # ... and nothing protected: no user sentence, prompt, request body, response body or credential.
    assert_clean(captured_text(sdk_debug))


@pytest.mark.parametrize('cls', PROVIDERS)
def test_default_client_loopback_http_error_logs_no_protected_content(cls, loopback, default_provider, sdk_debug):
    loopback.status, loopback.payload = 400, error_payload(400)
    provider = default_provider(cls, loopback.base_url)

    with pytest.raises(LLMUpstreamError) as info:
        provider.generate(messages(), response_schema=MemoryCandidate)

    assert str(info.value) == 'LLM provider returned an error'
    assert len(loopback.requests) == 1 and loopback.requests[0]['body'] == expected_body(cls)
    _connection, http11 = assert_real_httpcore2_logging(sdk_debug, loopback.port)
    assert any(m.startswith('receive_response_headers.complete') and '400' in m for m in http11)
    assert_openai_records_redacted(sdk_debug)
    assert_clean(captured_text(sdk_debug))


def test_default_client_loopback_connection_refused_logs_no_protected_content(default_provider, sdk_debug, no_sleep):
    with socket.socket() as probe:  # reserve a loopback port, then release it: nothing listens there
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    provider = default_provider(OpenAIChatProvider, f'http://127.0.0.1:{port}/v1')

    with pytest.raises(LLMTimeoutError) as info:
        provider.generate(messages(), response_schema=MemoryCandidate)

    assert type(info.value.__cause__) is openai.APIConnectionError
    connection, _http11 = assert_real_httpcore2_logging(sdk_debug, port)
    # Real transport failures, once per attempt: the SDK default retry policy is unchanged.
    assert sum(m.startswith('connect_tcp.failed') for m in connection) == 3
    assert_openai_records_redacted(sdk_debug)
    assert_clean(captured_text(sdk_debug))


# ------------------------------------------------ full Memory proposal API path

@pytest.mark.parametrize('cls', PROVIDERS)
def test_memory_proposal_api_over_real_sdk_path_logs_nothing_personal(cls, client, store, sdk_debug, tmp_path):
    sentence = 'Remember that I prefer 173-minute study sessions.'
    reply = json.dumps(cand('remember', 'preferred_session_duration', minutes=173, evidence=sentence))
    transport = Transport(lambda request: httpx2.Response(200, json=completion(reply)))
    provider = real_provider(cls, transport)
    app.dependency_overrides[get_llm_provider] = lambda: provider

    response = client.post(PROPOSALS, json={'content': sentence})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body['status'] == 'READY' and body['proposal']['new_value'] == 173
    assert len(transport.requests) == 1
    sent = sent_body(transport.requests[0])['messages']
    assert sent[-2:] == build_memory_candidate_messages(sentence)
    assert len(store) == 1
    assert rows(tmp_path, 'personal_memories') == [] and rows(tmp_path, 'memory_confirmation_receipts') == []
    assert_openai_records_redacted(sdk_debug)
    text = captured_text(sdk_debug)
    assert sentence not in text and '173-minute' not in text and '"evidence_text"' not in text
    assert_clean(text)


# ------------------------------------------- synthetic defence-in-depth control

def synthetic_body_record_args():
    return ({'json_data': {'model': MODEL, 'messages': messages()},
             'headers': {'Authorization': f'Bearer {KEY_SENTINEL}'}},)


@pytest.mark.parametrize('name', OPENAI_LOGGERS)
def test_synthetic_control_filter_redacts_a_body_bearing_record(name, sdk_debug):
    """SYNTHETIC defence-in-depth control (openai 3.24 does not emit this record itself).

    A body-bearing record is injected through the real logging pipeline of each protected SDK
    logger. Filter installed -> the sentinels are gone. Filter removed -> the same detector sees
    them, so the privacy assertions in this module are not vacuous.
    """
    protect_sdk_loggers()
    sdk_logger = logging.getLogger(name)
    installed = [f for f in sdk_logger.filters if isinstance(f, SdkLogRedactionFilter)]
    assert len(installed) == 1

    sdk_logger.debug('Request options: %s', *synthetic_body_record_args())

    assert [r.getMessage() for r in named(sdk_debug, name)] == [REDACTED_MESSAGE]
    assert_clean(captured_text(sdk_debug))

    sdk_debug.clear()
    sdk_logger.removeFilter(installed[0])
    try:
        sdk_logger.debug('Request options: %s', *synthetic_body_record_args())
        text = captured_text(sdk_debug)
        assert USER_SENTINEL in text and PROMPT_SENTINEL in text and BODY_SENTINEL in text
        with pytest.raises(AssertionError):
            assert_clean(text)
    finally:
        sdk_logger.addFilter(installed[0])
    assert sum(isinstance(f, SdkLogRedactionFilter) for f in sdk_logger.filters) == 1


@pytest.mark.parametrize('name', ['httpx2', 'httpcore2.connection', 'httpcore2.http11'])
def test_synthetic_control_detector_sees_transport_loggers(name, sdk_debug):
    """SYNTHETIC: the transport loggers carry no JARVIS filter, and the detector does watch them."""
    transport_logger = logging.getLogger(name)
    assert not any(isinstance(f, SdkLogRedactionFilter) for f in transport_logger.filters)

    transport_logger.debug('synthetic %s', USER_SENTINEL)

    with pytest.raises(AssertionError):
        assert_clean(captured_text(sdk_debug))


# ------------------------------------------------------------ filter mechanics

def test_protection_is_idempotent_and_covers_sdk_emitting_loggers():
    protect_sdk_loggers()
    protect_sdk_loggers()
    OpenAIChatProvider(api_key=KEY_SENTINEL, model='m', base_url=BASE_URL)
    DeepSeekChatProvider(api_key=KEY_SENTINEL, model='m', base_url=BASE_URL)
    for name in OPENAI_LOGGERS:
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, SdkLogRedactionFilter)]
        assert len(filters) == 1, name
    # The SDK's own header filter is left in place; transport loggers are not touched.
    assert any(type(f).__name__ == 'SensitiveHeadersFilter' for f in logging.getLogger('openai._base_client').filters)
    for name in TRANSPORT_LOGGERS + LEGACY_LOGGERS:
        assert not any(isinstance(f, SdkLogRedactionFilter) for f in logging.getLogger(name).filters), name


def test_redaction_rewrites_message_and_drops_args_exception_and_stack_payloads():
    try:
        raise ValueError(USER_SENTINEL)
    except ValueError:
        record = logging.LogRecord('openai._base_client', logging.DEBUG, __file__, 1, 'Request options: %s',
                                   synthetic_body_record_args(), sys.exc_info())
    record.exc_text = f'Traceback ... ValueError: {USER_SENTINEL}'
    record.stack_info = f'Stack (most recent call last): {USER_SENTINEL}'

    assert SdkLogRedactionFilter().filter(record) is True  # the record is kept, only its content is dropped

    assert record.msg == REDACTED_MESSAGE and record.getMessage() == REDACTED_MESSAGE
    assert record.args == ()
    assert record.exc_info is None and record.exc_text is None and record.stack_info is None
    assert (record.name, record.levelno, record.lineno) == ('openai._base_client', logging.DEBUG, 1)
    formatted = logging.Formatter('%(name)s %(levelname)s %(message)s').format(record)
    assert formatted == f'openai._base_client DEBUG {REDACTED_MESSAGE}'
    assert_clean(formatted)


def test_protection_does_not_change_client_retry_or_timeout_configuration():
    unprotected = openai.OpenAI(api_key=KEY_SENTINEL, base_url=BASE_URL, timeout=5)
    provider = OpenAIChatProvider(api_key=KEY_SENTINEL, model=MODEL, base_url=BASE_URL, timeout_seconds=5)
    try:
        assert provider._client.max_retries == unprotected.max_retries == openai.DEFAULT_MAX_RETRIES
        assert provider._client.timeout == unprotected.timeout == 5
        assert str(provider._client.base_url) == str(unprotected.base_url)
    finally:
        unprotected.close()
        provider._client.close()


# ------------------------------------------------------------------- embeddings

EMBED_DIMENSION = 512
EMBED_MODEL = 'text-embedding-3-small'


def embedding_provider(transport, *, sleeps, max_retries=1):
    provider = OpenAIEmbeddingProvider(
        dimension=EMBED_DIMENSION, model=EMBED_MODEL, api_key=KEY_SENTINEL, base_url=BASE_URL,
        timeout_seconds=5, max_retries=max_retries, retry_backoff_seconds=0.5, retry_sleeper=sleeps.append)
    # Real SDK client built by the provider itself; only the transport is mocked.
    provider._client = provider._get_client().with_options(
        http_client=httpx2.Client(transport=httpx2.MockTransport(transport)))
    return provider


def test_embedding_client_construction_installs_protection():
    target = logging.getLogger('openai._base_client')
    for f in [f for f in target.filters if isinstance(f, SdkLogRedactionFilter)]:
        target.removeFilter(f)
    provider = OpenAIEmbeddingProvider(dimension=1536, model=EMBED_MODEL, api_key=KEY_SENTINEL, base_url=BASE_URL)
    provider._get_client()
    provider._get_client()
    protect_sdk_loggers()
    assert sum(isinstance(f, SdkLogRedactionFilter) for f in target.filters) == 1


def test_embedding_real_sdk_path_behaviour_unchanged_and_logs_clean(sdk_debug):
    texts = [f'{USER_SENTINEL} one', f'{USER_SENTINEL} two']
    vectors = [[0.25] * EMBED_DIMENSION, [0.5] * EMBED_DIMENSION]
    reply = {'object': 'list', 'model': EMBED_MODEL, 'usage': {'prompt_tokens': 2, 'total_tokens': 2},
             # Out of order on purpose: the provider must still reorder by index.
             'data': [{'object': 'embedding', 'index': 1, 'embedding': vectors[1]},
                      {'object': 'embedding', 'index': 0, 'embedding': vectors[0]}]}
    transport = Transport(lambda request: httpx2.Response(200, json=reply))
    sleeps: list[float] = []
    provider = embedding_provider(transport, sleeps=sleeps)

    assert provider.embed_documents(texts) == vectors

    assert len(transport.requests) == 1 and sleeps == []
    request = transport.requests[0]
    assert str(request.url) == f'{BASE_URL}/embeddings'
    assert request.headers['authorization'] == f'Bearer {KEY_SENTINEL}'
    body = sent_body(request)
    assert (body['model'], body['input'], body['dimensions']) == (EMBED_MODEL, texts, EMBED_DIMENSION)
    assert_openai_records_redacted(sdk_debug)
    assert len(named(sdk_debug, 'httpx2')) == 1
    assert_clean(captured_text(sdk_debug))


def test_embedding_real_sdk_path_error_and_retry_behaviour_unchanged(sdk_debug, no_sleep):
    rejected = Transport(lambda request: httpx2.Response(401, json=error_payload(401)))
    sleeps: list[float] = []
    with pytest.raises(EmbeddingError) as info:
        embedding_provider(rejected, sleeps=sleeps).embed_query(USER_SENTINEL)
    assert info.value.code == 'EMBEDDING_INVALID_KEY'
    assert len(rejected.requests) == 1 and sleeps == []  # 401: no SDK retry, no application retry

    failing = Transport(lambda request: httpx2.Response(500, json=error_payload(500)))
    with pytest.raises(EmbeddingError) as info:
        embedding_provider(failing, sleeps=sleeps, max_retries=1).embed_query(USER_SENTINEL)
    assert info.value.code == 'EMBEDDING_UPSTREAM_ERROR'
    assert sleeps == [0.5]  # one application-level retry, back-off unchanged
    # Each application attempt is one SDK call, and the SDK default policy sends 1 + 2 retries.
    assert len(failing.requests) == 2 * (openai.DEFAULT_MAX_RETRIES + 1)
    assert RAW_SENTINEL not in str(info.value) and USER_SENTINEL not in str(info.value)
    assert_openai_records_redacted(sdk_debug)
    assert_clean(captured_text(sdk_debug))


# ------------------------------------------- legacy httpx compatibility (not the main proof)

def test_legacy_httpx_client_remains_supported_and_logs_clean(sdk_debug):
    seen: list[httpx.Request] = []

    def respond(request):
        request.read()
        seen.append(request)
        return httpx.Response(200, json=completion(f'{{"note": "{RAW_SENTINEL}"}}'))

    provider = OpenAIChatProvider(api_key=KEY_SENTINEL, model=MODEL, base_url=BASE_URL, timeout_seconds=5)
    provider._client = provider._client.with_options(http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    result = provider.generate(messages(), response_schema=MemoryCandidate)

    assert result.text == f'{{"note": "{RAW_SENTINEL}"}}'
    assert len(seen) == 1 and json.loads(seen[0].content) == expected_body(OpenAIChatProvider)
    # Legacy httpx / httpcore records are dropped at source by Phase 9A secure_library_logging()
    # (installed when app.main is imported); that list does not cover httpx2 / httpcore2.
    assert not named(sdk_debug, *LEGACY_LOGGERS) and not named(sdk_debug, *TRANSPORT_LOGGERS)
    assert_openai_records_redacted(sdk_debug)
    assert_clean(captured_text(sdk_debug))
