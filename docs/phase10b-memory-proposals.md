# Explicit Memory Proposal & Confirmation — Phase 10B

**Status: CLOSED / ACCEPTED** (Astra, 2026-10-06). The Real Provider Gate and the
Real DB Gate both passed; the real `backend/jarvis.db` is at this phase's revision
`ae1f2a3b4c5d`. See [Final acceptance](#final-acceptance) for the recorded evidence.

Earlier revisions of this document (implementation checkpoint `9040c83`) described the
phase as implemented but not closed, with both gates pending. That was accurate at the
time; the gates were run afterwards, in the order recorded below.

## Flow

```
Memory tab sentence → frozen V1 grammar (full match only) → constrained LLM candidate
→ strict schema → deterministic policy → exact-key Memory lookup → Preview (RAM)
→ explicit Confirm → Memory mutation + receipt in ONE transaction
```

The **记忆** tab is the only entry point. Chat, Action Proposal, RAG, Task and
Calendar never write Memory. Manual CRUD from Phase 10A is unchanged, except that
a manual update now always resets `source_type` to `manual`.

## Supported keys (natural-language flow only)

| key | category | value |
| --- | --- | --- |
| `preferred_study_time` | preference | `"morning"` / `"afternoon"` / `"evening"` |
| `preferred_session_duration` | preference | JSON integer 5–180 (minutes) |
| `usual_available_days` | constraint | deduplicated `mon`…`sun`, Monday → Sunday, 1–7 items |

Only the 14 frozen Chinese/English forms (8 remember, 6 explicit replace) can
produce READY; see `backend/app/services/memory_grammar.py`. Everything else fails
closed deterministically as `UNSUPPORTED`, `NEEDS_CLARIFICATION` or `INVALID`
(for example an out-of-range duration). Inputs that do not match the grammar are
not sent to the LLM.

### Astra ratification (narrow)

- Grammar runs before the LLM: approved.
- Malformed model candidate → `INVALID` / `MEMORY_PROPOSAL_INVALID` (HTTP 200): approved.
- **Grammar matched but the candidate disagrees** (intent, key or value, or the model returns
  `clarify` / `unsupported`) → `INVALID` / `MEMORY_PROPOSAL_INVALID`. There is no READY fallback to the
  grammar result, no retry and no repair call. User ambiguity stays `NEEDS_CLARIFICATION`;
  a model failure after an unambiguous grammar match is `INVALID`.
- English night stays narrow: only "Remember that I usually study at night"; "in the night"
  → `NEEDS_CLARIFICATION` without an LLM call.
- Sunday aliases stay narrow: 周日 / 星期日 / Sunday / Sun; 周天 / 星期天 → `NEEDS_CLARIFICATION`
  without an LLM call.

### Astra review fixes

Both review items are **closed**.

- **R1 — English case (closed).** English matching is case-insensitive over ASCII letters only
  (`re.IGNORECASE | re.ASCII`), and the weekday list is split with the same flags, so
  `MONDAY AND WEDNESDAY` / `Monday And Wednesday` canonicalize to `["mon", "wed"]`.
  Unicode case-fold variants (e.g. `ſ` U+017F, `ı` U+0131, `K` U+212A) never match and
  fail closed before the LLM. Canonical lookup never trusts a regex match: an unknown token
  is not a frozen match and fails closed (`NEEDS_CLARIFICATION`), never a `KeyError` / 500.
  Vocabulary is unchanged.
- **R2 — SDK DEBUG logging (closed, with the accepted residual below).** Validated runtime: Python 3.12.10, openai 3.24.0, whose
  default HTTP stack is httpx2 2.13.1 → httpcore2 2.13.1 (no longer httpx / httpcore).
  - *Historical behaviour.* Earlier openai SDK versions logged the full request options
    (messages: the sentence and the Memory prompt) at DEBUG. The exact historical version
    was not reconstructed.
  - *Current behaviour.* openai 3.24.0 logs metadata only on `openai.*` (HTTP method,
    status, request id, exception type name, retry counters), not request or response
    bodies. Its own filter only hides the Authorization / api-key request headers.
  - *`app/llm/sdk_logging.py` is defence in depth.* It adds a filter to the SDK's emitting
    loggers (`openai`, `openai._base_client`, `openai._response`, `openai._legacy_response`,
    plus any existing `openai.*`) that rewrites each record to a fixed text and drops args,
    exception and stack payloads; nothing is stored elsewhere. It is installed, idempotently,
    when an OpenAI-compatible chat (OpenAI / DeepSeek) or embedding client is built. It does
    not touch requests, responses or the SDK retry policy.
  - *Transport loggers are not filtered.* `httpx2` (INFO) logs method, full URL and status
    line. `httpcore2.connection` / `httpcore2.http11` (DEBUG) log connection parameters, the
    request repr (method only), exception reprs and the **full response headers**.
  - *Accepted residual (Astra, Phase 10B).* The httpcore2 response-header DEBUG logging is
    not redacted. Response headers may contain request IDs, rate-limit metadata, cookies or
    provider-specific fields, so **raw DEBUG logs must not be treated as public or shareable
    evidence**. Tests show no sentence, prompt, request body, response body, API key or
    Authorization value on this path.
  - *Legacy httpx.* An explicitly injected legacy `httpx.Client` is still accepted by the
    SDK. Phase 9A `secure_library_logging()` silences the legacy `httpx` / `httpcore`
    loggers only; it does not cover `httpx2` / `httpcore2`.
  - *Verification* (`backend/tests/test_phase10b_sdk_log_privacy.py`, offline, synthetic
    keys and content): the real providers and SDK over native `httpx2.MockTransport`, and
    the SDK's default client against a temporary `127.0.0.1` server, which exercises the
    real httpcore2 logging. The filter control injects a **synthetic** body-bearing record;
    it does not claim that openai 3.24 emits one.

## Generation outcomes (HTTP 200)

`READY` (with a `proposal_id`; the server decides CREATE vs UPDATE), `ALREADY_SAVED`
(same type-sensitive value already stored: no write), `NEEDS_CLARIFICATION`,
`UNSUPPORTED`, `INVALID`. "Remember" with a different stored value, or a stored value
with an incompatible category/shape, needs clarification. There is no implicit
upsert and no migration of old free-form values.

## API

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/api/memories/proposals` | `{"content": "..."}` — trimmed 1–1000 chars, ≤ 4096 UTF-8 bytes, single line, no control chars |
| POST | `/api/memories/proposals/{id}/confirm` | body `{}` only → `APPLIED` + `replayed` |
| DELETE | `/api/memories/proposals/{id}` | `CANCELLED`, or the historical `APPLIED` result |

All three respond with `Cache-Control: no-store`. There is no endpoint that lists proposals.
Errors: `MEMORY_PROPOSAL_EXPIRED` 410, `MEMORY_KEY_CONFLICT` / `MEMORY_VERSION_CONFLICT`
409, `MEMORY_TARGET_NOT_FOUND` 404, `MEMORY_PROPOSAL_CAPACITY_EXCEEDED` /
`MEMORY_STORAGE_ERROR` / `LLM_NOT_CONFIGURED` 503, `LLM_TIMEOUT` / `LLM_UPSTREAM_ERROR`
502, `VALIDATION_ERROR` 422. Error responses never echo the sentence, values or model output.

## Pending proposals (in-process RAM only)

UUID4 IDs, 10-minute TTL (monotonic clock), at most 128 per backend process
(no eviction of valid proposals), expiry checked on access and swept about every
30 s. Only what Confirm needs is kept: never the sentence, the prompt or the raw
model response. A **restart loses pending proposals**, and a page refresh does not
restore a preview. V1 supports a single API process.

## Confirmation receipts and idempotency

`memory_confirmation_receipts(proposal_id PK, operation, memory_id, applied_version,
status='APPLIED', confirmed_at)`. No key, category, value, sentence or prompt is
stored, and there is no foreign key, so a receipt survives Memory deletion. Confirm checks the
receipt **first**: a confirmed proposal replays `replayed=true` after expiry, restart,
update or delete, without mutating or recreating Memory. One logical mutation
is guaranteed; one HTTP request is not. If a failure leaves the outcome
uncertain, the same proposal ID remains retryable.

Conflicts invalidate the proposal. They apply even if the latest value happens to
equal the proposed value.

## `source_type`

`manual` (direct CRUD) and `user_confirmed` (confirmed proposal). This is
server-controlled only.

## Migration

`ae1f2a3b4c5d` (parent `9d0e1f2a3b4c`) expands the `source_type` CHECK and creates the
receipts table. SQLite recreates `personal_memories` and preserves rows, IDs,
timestamps, constraints, the index, AUTOINCREMENT and the `sqlite_sequence`
high-water mark. PostgreSQL alters the CHECK in place (verified offline only).
Downgrade is for disposable test databases only and refuses while any
`user_confirmed` Memory or receipt exists.

## Known pre-existing limitation: SQLite numeric type loss

`personal_memories.value_json` (Phase 10A) is declared `JSON`, which SQLite gives
NUMERIC affinity. A historical manually stored `45.0` is therefore read back as the
integer `45`. Phase 10B applies strict typing to candidate and input values
(a candidate `45.0`, a read float, a bool or `"45"` all fail the integer duration
contract) and compares the **actual read value**. It cannot recover type information
that SQLite already lost. This is not fixed by Phase 10B; it is a future
storage-model consideration.

## Out of scope

Natural-language delete, context injection (Phase 10C), vector/semantic memory,
automatic memory, Planning, and distributed proposal storage.

## Final acceptance

Recorded in the order the work happened. Phase 10B is **CLOSED / ACCEPTED**.

1. **Implementation checkpoint** — commit `9040c83`. Implementation review: PASS.
   At that point both gates were pending and the real database was still at `9d0e1f2a3b4c`.
2. **Fresh-environment validation (2026-10-05).** The restored environment (Python 3.12.10,
   openai 3.24.0, httpx2 2.13.1, httpcore2 2.13.1) exposed pre-existing dependency manifest
   defects: three runtime dependencies were used but never declared. They were added to
   `backend/requirements.txt` and installed from the manifest:
   - `python-multipart` 0.0.32 — Documents API upload (route registration fails without it);
   - `pypdf` 6.19.0 and `python-docx` 1.2.0 (with `lxml` 6.1.3) — document parsers.

   No parser, document-service or Memory code changed. The SDK privacy tests were rewritten
   for the openai 3.24 stack (R2 above); `app/llm/sdk_logging.py` kept its logic and remains
   defence in depth, and the httpcore2 response-header DEBUG residual was accepted.
3. **Final regression (fresh environment).** Full backend suite: **1479 collected, 1442
   passed, 37 skipped, 0 failed, 0 errors, exit code 0.** The 37 skips are gated real-PostgreSQL
   and real-provider smokes.
4. **Real Provider Gate — PASS (2026-10-05).** Provider `deepseek`, model `deepseek-v4-flash`,
   against a disposable database at `ae1f2a3b4c5d`. Authorized real business calls: 2;
   observed: 2; SDK transport retries: 0. "Remember that I prefer 45-minute study sessions."
   produced READY / CREATE `preferred_session_duration` = 45, and "Change my preferred session
   duration to 60 minutes." produced READY / UPDATE = 60 on the same Memory. Neither generation
   wrote to the database; each confirmation applied one Memory mutation and one receipt.
5. **Real DB Gate — PASS.** The real `backend/jarvis.db` was migrated from `9d0e1f2a3b4c` to
   `ae1f2a3b4c5d` under explicit authorization. Final audit state:
   - `personal_memories` rows: 0
   - `memory_confirmation_receipts` rows: 2 (one `CREATE`, one `UPDATE`, both `APPLIED`)
   - `personal_memories` AUTOINCREMENT high-water (`sqlite_sequence`): 2
   - SQLite `quick_check`: `ok`
   - SHA-256 at the end of the gate (point-in-time evidence, not a specification):
     `6594741e55809557a7133dff5f9477cb0562bb3e4c9f4199c4e797bfa057c842`

   The gate's backups and evidence are kept outside the repository and are not copied into it.

### Receipt retention

The two receipts in the real database are **intentional historical audit records** of the
Real DB Gate. Receipts have no foreign key and deliberately outlive the Memory they refer to,
which is why they remain while `personal_memories` is empty. They must not be deleted, and
the AUTOINCREMENT high-water mark must not be reset, merely to make the database look clean.

### Accepted limitations at close

Not blockers for Phase 10B: the SQLite numeric type loss above; pending proposals held in a
single process's RAM; the httpcore2 response-header DEBUG residual; Phase 9A logging
suppression covering only legacy httpx / httpcore; no full dependency lock strategy; and no
execution of Phase 10B against a real PostgreSQL database.
