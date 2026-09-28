# Personal Memory — Phase 10A

Memory is a manually managed collection of relatively stable preferences,
background facts and recurring personal constraints. The **记忆** tab supports
create, list/filter, edit, refresh and confirmed deletion. Chat, Action Proposal,
RAG and workers neither capture memory nor inject it into model context.

Examples: `preferred_study_time`, `preferred_session_duration`,
`timezone_preference`, `dietary_preference`. Do not store passwords, API keys,
authentication codes or hidden model reasoning. There is no secret classifier.

## Domain boundary

Do not save “task 5 completed”, “meeting moved to 3 pm”, “reminder sent” or
“execution 10 succeeded” here. Task DB, Reminder DB, Microsoft Calendar,
Execution DB and Document/Retrieval domain remain their respective sources of
truth. Memory does not validate or synchronize claims against those domains.

## HTTP contract

| Method | Path | Result |
| --- | --- | --- |
| POST | `/api/memories` | 201, current memory |
| GET | `/api/memories?category=preference` | 200, list sorted by category then key; filter optional |
| GET | `/api/memories/{id}` | 200 or safe 404 |
| PUT | `/api/memories/{id}` | Full replacement of editable fields, 200 |
| DELETE | `/api/memories/{id}` | 200 `{"deleted":true}`; missing ID is 404 |

Create accepts exactly `category`, `key`, `value`. Update additionally requires
positive integer `expected_version`. Unknown fields are rejected, including
client-provided source, version, timestamps and tool/handler names. Create uses
version 1 and server-controlled `source_type=manual`. Timestamps are server
clock UTC, emitted with `Z`. Updates increment version atomically through a
database predicate on ID and expected version, preserving creation time.
There is no silent overwrite and no automatic conflict retry.

Categories: `preference`, `stable_fact`, `constraint` only. Key: 1–80 ASCII
characters, starts with a lowercase letter, then lowercase letters, digits or
underscores. Key is globally unique within this single-user local application.

Value accepts string, integer, finite float, boolean, a list of at most 64
scalars, or a flat object with at most 32 scalar fields. Object field names are
1–80 characters without ASCII controls. Null, nonfinite numbers, binary/Python
objects and nested containers are rejected. Compact JSON with literal Unicode
must be at most 4096 UTF-8 bytes. Values are inert data; nothing evaluates code
or SQL supplied as text. The UI supplies typed inputs rather than requiring
raw JSON. The browser number editor rejects integers outside JavaScript's safe
integer range; use text for identifiers needing more digits.

Duplicate keys return 409 `MEMORY_KEY_CONFLICT`. Stale updates return 409
`MEMORY_VERSION_CONFLICT`; the UI requires an explicit reload before another
save. Missing rows return 404 `MEMORY_NOT_FOUND`. Validation failures return
422 `VALIDATION_ERROR`. Database failures return a safe 503 without SQL or
values. Memory operations do not log values or create execution/audit copies.

Delete removes the current row. Updates replace the current value. No history,
tombstone, undo or old-value archive is maintained by this domain. Application
hard deletion is **not** cryptographic secure erasure of storage or backups.

## Schema and rollout

`personal_memories`: integer ID, category VARCHAR(20), key VARCHAR(80), portable
JSON value_json, source_type VARCHAR(20), integer version and UTC-aware
created_at/updated_at. All columns are non-null. Primary key on ID, unique key,
category/source/version check constraints and a `(category,key)` index.
No foreign keys to other domains; no new dependencies.
SQLite uses monotonic AUTOINCREMENT IDs so a stale editor cannot target a new
memory that reused a deleted ID. PostgreSQL uses its native integer sequence.

Exactly one migration extends `8c9d0e1f2a3b4` to `9d0e1f2a3b4c`.
Downgrade removes only the memory index and table (and its current data).
Alembic remains the sole schema authority. Startup does not create, migrate,
stamp or repair databases. Revision readiness is not a full physical schema
diff or a proof against a database administrator's incorrect stamp.

**Implementation gate (historical, implementation-time state):** at that time
the real `backend/jarvis.db` remained at the old head. The
application was not started against it and it was not migrated in that gate.
The real migration required the separately reviewed rollout gate. All tests
and UI verification used explicitly named disposable databases.

**Current accepted state:** the real DB migration gate subsequently PASSED.
The current real `backend/jarvis.db` revision is `9d0e1f2a3b4c`. The real
Memory CRUD smoke PASSED, and the final test memory was deleted (0 rows).

This is a local single-user feature using the existing deployment boundary.
It adds no multi-user isolation or public deployment authentication. Automatic
extraction, proposals, context injection, vectors, planning and Phase 10B are
not implemented.
