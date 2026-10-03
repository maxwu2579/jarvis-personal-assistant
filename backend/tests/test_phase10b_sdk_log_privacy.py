"""Phase 10B Astra R2: openai SDK DEBUG logging must not emit request content.

Offline only: the real OpenAIChatProvider / DeepSeekChatProvider and the real
openai SDK client run against httpx.MockTransport (no network; base URL is a
non-resolvable .invalid host). No Fake Provider is used for the privacy proof.
"""

import json
import logging

import httpx
import openai
import pytest

from app.embeddings.openai_provider import OpenAIEmbeddingProvider
from app.llm.base import LLMTimeoutError, LLMUpstreamError
from app.llm.deepseek_provider import DeepSeekChatProvider
from app.llm.factory import get_llm_provider
from app.llm.memory_prompts import MEMORY_CANDIDATE_SYSTEM_PROMPT, build_memory_candidate_messages
from app.llm.openai_provider import OpenAIChatProvider
from app.llm.sdk_logging import REDACTED_MESSAGE, SdkLogRedactionFilter, protect_sdk_loggers
from app.main import app
from app.services.memory_proposal_service import MemoryCandidate
from tests.test_phase10b import PROPOSALS, cand, rows
from tests.test_phase10b import mono, store  # noqa: F401  (pytest fixtures)

BASE_URL = 'https://sdk-privacy.invalid/v1'
KEY_SENTINEL = 'sk-KEY_SENTINEL_10B_R2'
USER_SENTINEL = 'USER_SENTENCE_SENTINEL_10B_R2 I usually study on the moon'
RAW_SENTINEL = 'RAW_RESPONSE_SENTINEL_10B_R2'
PROMPT_SENTINEL = 'convert ONE user sentence'  # distinctive text of the Memory system prompt
SDK_LOGGERS = ('openai', 'openai._base_client', 'openai._response', 'httpx', 'httpcore')
PROVIDERS = [OpenAIChatProvider, DeepSeekChatProvider]


def completion(content, *, completion_id=f'chatcmpl-{RAW_SENTINEL}'):
    return {
        'id': completion_id, 'object': 'chat.completion', 'created': 1, 'model': 'mock-model',
        'system_fingerprint': RAW_SENTINEL,
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': content}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 4, 'total_tokens': 7},
    }


class Transport:
    """Records every request the SDK sends; answers with a scripted response."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        request.read()
        self.requests.append(request)
        return self.respond(request)


def real_provider(cls, transport):
    provider = cls(api_key=KEY_SENTINEL, model='mock-model', base_url=BASE_URL, timeout_seconds=5)
    # Same SDK client (key, base URL, timeout, default retry policy); only the transport is mocked.
    provider._client = provider._client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(transport))
    )
    return provider


@pytest.fixture()
def sdk_debug(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    for name in SDK_LOGGERS + ('openai._legacy_response',):
        caplog.set_level(logging.DEBUG, logger=name)
        # alembic's fileConfig (run by migration tests) disables existing loggers;
        # re-enable them so the SDK path really emits and the proof is not vacuous.
        monkeypatch.setattr(logging.getLogger(name), 'disabled', False)
    return caplog


@pytest.fixture()
def no_sleep(monkeypatch):
    # Retry policy untouched; only the SDK's back-off wait is skipped for speed.
    monkeypatch.setattr(openai._base_client.time, 'sleep', lambda _seconds: None)


def assert_clean(text):
    for sentinel in (USER_SENTINEL, 'USER_SENTENCE_SENTINEL', PROMPT_SENTINEL, RAW_SENTINEL,
                     KEY_SENTINEL, 'Bearer', 'json_data', '"messages"', "'messages'", 'response_format'):
        assert sentinel not in text, sentinel


def sdk_records(caplog):
    return [r for r in caplog.records if r.name == 'openai' or r.name.startswith('openai.')]


def messages():
    return [{'role': 'system', 'content': MEMORY_CANDIDATE_SYSTEM_PROMPT},
            {'role': 'user', 'content': USER_SENTINEL}]


def sent_body(request):
    return json.loads(request.content)


# ------------------------------------------------------------ SDK path, success

@pytest.mark.parametrize('cls', PROVIDERS)
def test_success_path_debug_logs_carry_no_request_or_response_content(cls, sdk_debug):
    transport = Transport(lambda request: httpx.Response(200, json=completion(f'{{"note": "{RAW_SENTINEL}"}}')))
    provider = real_provider(cls, transport)

    result = provider.generate(messages(), response_schema=MemoryCandidate)

    # Behaviour unchanged: the provider still sent the intended request and parsed the reply.
    assert result.text == f'{{"note": "{RAW_SENTINEL}"}}' and result.prompt_tokens == 3
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert str(request.url) == f'{BASE_URL}/chat/completions'
    assert request.headers['authorization'] == f'Bearer {KEY_SENTINEL}'
    body = sent_body(request)
    assert body['messages'][-1] == {'role': 'user', 'content': USER_SENTINEL}
    assert any(m['content'] == MEMORY_CANDIDATE_SYSTEM_PROMPT for m in body['messages'])
    assert 'response_format' in body

    # The SDK really logged at DEBUG (the path is exercised) — but only redacted records.
    records = sdk_records(sdk_debug)
    assert any(r.name == 'openai._base_client' and r.levelno == logging.DEBUG for r in records)
    assert all(r.getMessage() == REDACTED_MESSAGE and r.exc_info is None for r in records)
    assert_clean(sdk_debug.text)


# -------------------------------------------------------------- SDK path, errors

@pytest.mark.parametrize('cls', PROVIDERS)
def test_http_error_path_debug_logs_carry_no_content(cls, sdk_debug):
    error_body = {'error': {'message': f'bad request {RAW_SENTINEL} {USER_SENTINEL}', 'type': 'invalid_request_error'}}
    transport = Transport(lambda request: httpx.Response(400, json=error_body))
    provider = real_provider(cls, transport)

    with pytest.raises(LLMUpstreamError) as info:
        provider.generate(messages(), response_schema=MemoryCandidate)

    assert str(info.value) == 'LLM provider returned an error'
    assert len(transport.requests) == 1  # 400 is not retried by the SDK default policy
    assert sent_body(transport.requests[0])['messages'][-1]['content'] == USER_SENTINEL
    assert any(r.name == 'openai._base_client' for r in sdk_records(sdk_debug))
    assert_clean(sdk_debug.text)


@pytest.mark.parametrize('cls', PROVIDERS)
def test_connection_error_path_with_sdk_retries_carries_no_content(cls, sdk_debug, no_sleep):
    def refuse(request):
        raise httpx.ConnectError(f'refused {RAW_SENTINEL}', request=request)

    transport = Transport(refuse)
    provider = real_provider(cls, transport)

    with pytest.raises(LLMTimeoutError):
        provider.generate(messages(), response_schema=MemoryCandidate)

    # SDK default retry policy still applies (1 attempt + 2 retries), unchanged by the fix.
    assert len(transport.requests) == 3
    records = sdk_records(sdk_debug)
    assert any(r.levelno == logging.INFO for r in records)  # "Retrying request ..." (redacted)
    assert all(r.exc_info is None and r.exc_text is None for r in records)
    assert_clean(sdk_debug.text)


# ------------------------------------------------ full Memory proposal API path

@pytest.mark.parametrize('cls', PROVIDERS)
def test_memory_proposal_api_over_real_sdk_path_logs_nothing_personal(cls, client, store, sdk_debug, tmp_path):
    sentence = 'Remember that I prefer 173-minute study sessions.'
    reply = json.dumps(cand('remember', 'preferred_session_duration', minutes=173, evidence=sentence))
    transport = Transport(lambda request: httpx.Response(200, json=completion(reply)))
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
    text = sdk_debug.text
    assert sentence not in text and '173-minute' not in text and '"evidence_text"' not in text
    assert_clean(text)


# ------------------------------------------------------------ filter mechanics

def test_unprotected_sdk_logger_would_leak_proving_the_test_is_sensitive(sdk_debug):
    """Control: with the redaction filter removed, the same SDK path logs the body."""
    transport = Transport(lambda request: httpx.Response(200, json=completion('{}')))
    provider = real_provider(OpenAIChatProvider, transport)
    removed = []
    for name in list(logging.Logger.manager.loggerDict):
        if name == 'openai' or name.startswith('openai.'):
            sdk_logger = logging.getLogger(name)
            for f in list(sdk_logger.filters):
                if isinstance(f, SdkLogRedactionFilter):
                    sdk_logger.removeFilter(f)
                    removed.append((sdk_logger, f))
    try:
        provider.generate(messages())
        assert USER_SENTINEL in sdk_debug.text and PROMPT_SENTINEL in sdk_debug.text
        # The SDK's own header filter still hides the key; the body is what leaks.
        assert KEY_SENTINEL not in sdk_debug.text
    finally:
        for sdk_logger, f in removed:
            sdk_logger.addFilter(f)
    assert removed


def test_protection_is_idempotent_and_covers_sdk_emitting_loggers():
    protect_sdk_loggers()
    protect_sdk_loggers()
    OpenAIChatProvider(api_key=KEY_SENTINEL, model='m', base_url=BASE_URL)
    for name in ('openai', 'openai._base_client', 'openai._response', 'openai._legacy_response'):
        filters = [f for f in logging.getLogger(name).filters if isinstance(f, SdkLogRedactionFilter)]
        assert len(filters) == 1, name
    # The SDK's own header filter is left in place.
    assert any(type(f).__name__ == 'SensitiveHeadersFilter' for f in logging.getLogger('openai._base_client').filters)


def test_embedding_client_construction_installs_protection():
    target = logging.getLogger('openai._base_client')
    for f in [f for f in target.filters if isinstance(f, SdkLogRedactionFilter)]:
        target.removeFilter(f)
    provider = OpenAIEmbeddingProvider(dimension=1536, model='text-embedding-3-small', api_key=KEY_SENTINEL,
                                       base_url=BASE_URL)
    provider._get_client()
    assert sum(isinstance(f, SdkLogRedactionFilter) for f in target.filters) == 1


def test_redaction_drops_args_and_exception_payloads():
    try:
        raise ValueError(USER_SENTINEL)
    except ValueError:
        import sys
        record = logging.LogRecord('openai._base_client', logging.DEBUG, __file__, 1, 'Request options: %s',
                                   ({'json_data': {'messages': [USER_SENTINEL]}},), sys.exc_info())
    assert SdkLogRedactionFilter().filter(record) is True
    assert record.getMessage() == REDACTED_MESSAGE
    assert record.args == () and record.exc_info is None and record.exc_text is None
