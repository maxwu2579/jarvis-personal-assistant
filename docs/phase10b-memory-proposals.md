# Explicit Memory Proposal & Confirmation — Phase 10B

**Status: IMPLEMENTED — NOT CLOSED.** Pending Luna independent QA, the Real
Provider Gate and the Real DB Gate. The real `backend/jarvis.db` has **not** been
migrated to this phase's revision, and no real LLM provider has been called.

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

- **R1 — English case.** English matching is case-insensitive over ASCII letters only
  (`re.IGNORECASE | re.ASCII`), and the weekday list is split with the same flags, so
  `MONDAY AND WEDNESDAY` / `Monday And Wednesday` canonicalize to `["mon", "wed"]`.
  Unicode case-fold variants (e.g. `ſ` U+017F, `ı` U+0131, `K` U+212A) never match and
  fail closed before the LLM. Canonical lookup never trusts a regex match: an unknown token
  is not a frozen match and fails closed (`NEEDS_CLARIFICATION`), never a `KeyError` / 500.
  Vocabulary is unchanged.
- **R2 — SDK DEBUG logging.** The installed openai SDK logs the full request options
  (messages: the sentence and the Memory prompt) at DEBUG; its own filter only hides the
  Authorization / api-key headers. `app/llm/sdk_logging.py` adds a filter to the SDK's
  emitting loggers (`openai`, `openai._base_client`, `openai._response`,
  `openai._legacy_response`, plus any existing `openai.*`) that rewrites each record to a
  fixed text and drops args and exception payloads; nothing is stored elsewhere. It is
  installed when an OpenAI-compatible chat (OpenAI / DeepSeek) or embedding client is built.
  httpx / httpcore log only method, URL, status and headers repr, not bodies. Verified
  offline with the real provider and SDK over `httpx.MockTransport`.

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
