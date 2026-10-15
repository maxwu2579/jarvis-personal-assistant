# JARVIS Phase 11A — Today: Read-Only Daily Command Center — Frozen Specification

Status: FROZEN FOR SPEC ACKNOWLEDGEMENT; IMPLEMENTATION NOT AUTHORIZED.
Owner: Frozen Spec Owner (Astra, or a Principal Architect explicitly acting in that role). Drafted by the Phase 11A Specification Engineer from the owner's recorded architecture decisions.
Date: 2026-10-06.
Repository: C:\Users\25795\Desktop\Personal AI Assistant（JARVIS）
Accepted base: main, f25d06fe2460f19ddc15b82310d4ebafffc95dc4. Real DB revision: b10c0d1e2f3a.
This file is the authoritative implementation contract for Phase 11A. Phases 10B and 10C remain CLOSED / ACCEPTED; this specification reopens neither and authorizes no later phase.

## 1. Executive Decision

Build **Today**: one new default tab backed by one new deterministic read-only aggregation endpoint. It projects current Task, Reminder, Notification and Execution state for the user's local calendar day, and offers an explicit, user-triggered load of today's Outlook events through the existing Calendar read path.

| Decision | Frozen value |
| --- | --- |
| LLM / provider use | NONE |
| Persistence added | NONE (pure projection) |
| Migration | NO |
| New public API | YES — exactly one: GET /api/today |
| New write semantics | NONE — existing domain endpoints are reused unchanged |
| "Today" definition | The browser's IANA timezone, sent explicitly; local midnight to next local midnight |
| Microsoft Graph calls on Today load | ZERO |
| Calendar events | Loaded only after an explicit user click, through the existing GET /api/calendar/events |
| Reminder Worker status | Not detected, not inferred; disclosed as unknown |
| Memory / Chat / RAG involvement | NONE |

Today is not Planning and not an Agent. It never suggests, schedules, summarizes or decides.

## 2. Accepted Baseline

Verified at specification time against the code (the code, not this summary, is the implementation source of truth):
- HEAD f25d06fe2460f19ddc15b82310d4ebafffc95dc4, branch main, staged 0. Unrelated worktree state: modified .gitignore, untracked apisix-docker/. Preserve both.
- Task: statuses DRAFT, CONFIRMED, DONE only; `due_at` nullable. Transitions: confirm (DRAFT→CONFIRMED), complete (CONFIRMED→DONE), postpone (CONFIRMED only, sets a new `due_at`). No priority, tag, delete or edit.
- Reminder: statuses PENDING, CLAIMED, DELIVERED, CANCELLED, FAILED; `remind_at`; at most one PENDING/CLAIMED reminder per task; delivery writes one Notification. Delivery is performed only by the separately started Reminder Worker.
- Notification: one type (REMINDER_DUE); `read_at` null means unread; existing mark-read endpoint is idempotent.
- Execution: statuses CREATED, QUEUED, CLAIMED, RUNNING, SUCCEEDED, FAILED, CANCEL_REQUESTED, CANCELLED, TIMED_OUT. Terminal: SUCCEEDED, FAILED, CANCELLED, TIMED_OUT. Steps carry `requires_confirmation` and `confirmation_status` (PENDING, GRANTED, REJECTED, EXPIRED). Public types are `PUBLIC_EXECUTION_TYPES`; `action.confirm` is internal and never public. The public list API filters on `owner_id == DEFAULT_OWNER_ID`.
- Datetime storage: all domain datetimes are UTC. SQLite stores naive UTC, PostgreSQL stores aware UTC; `app.core.dialect.coerce_utc_for_db(value, DIALECT)` is the single write/compare seam and `app.schemas.common.serialize_utc` the single output seam. This is sufficient for the day-boundary contract in section 9; no datetime semantics change.
- Clock: `app.core.clock.get_clock` is the injectable source of "now".
- Calendar: `GET /api/calendar/connection` reads only the local encrypted token cache and makes no Graph request. `GET /api/calendar/events?start&end` and `GET /api/calendar/default` call Microsoft Graph. All `/api/calendar/*` routes are loopback-only. Event ranges up to 31 days are accepted.
- Frontend: six tabs; `initialTab()` returns `tasks` when no `tab` query parameter is present. No frontend test framework is installed. `frontend/node_modules` is restored from the existing lockfile.
- Existing tests read `frontend/src/App.tsx`, `types.ts` and `api.ts` as text. `backend/tests/test_phase9b.py` requires the string `calendar_execution` to remain in `App.tsx`. No existing test enumerates the route inventory.
- Local-first, single-user, loopback deployment; no authentication.

## 3. User Goal

Open JARVIS and immediately see what matters today without visiting the Task, Notification, Calendar and Execution screens one by one.

## 4. User Value

On opening the app the user sees, on one screen: tasks that are overdue, tasks due today, drafts awaiting confirmation, today's reminders, unread notifications and executions that need attention; and can complete, postpone or confirm a task, or mark a notification read, in place. Today's calendar is one click away and never blocks the rest.

## 5. Scope

- One new backend endpoint: GET /api/today (section 16).
- One new backend read service that queries the four local domains for one local day.
- One new frontend tab "今天", first in the tab bar and the default landing tab.
- Reuse of existing action endpoints for task confirm / complete / postpone and notification mark-read.
- An explicit "加载今日日程" action that calls the existing Calendar events endpoint for the same local-day range.
- Focused backend tests, frontend build/lint, bounded browser QA.

## 6. Explicit Non-Goals

No LLM. No AI summary. No "Plan My Day". No suggested schedule. No autonomous next action. No cross-domain write transaction. No new Reminder behavior. No snooze. No recurring reminders. No push, email or SMS. No worker heartbeat. No worker auto-start. No Calendar auto-fetch. No Calendar caching. No Calendar write change. No Memory consumption. No Chat integration. No RAG integration. No execution-result feedback loop. No persistent Today state. No migration. No new authentication model. No Phase 11B / 11C behavior.

Also out of scope: task editing or deletion, priorities, free/busy inference, notification grouping, polling or live updates on the Today tab, a date picker for other days, the pre-existing ChatView REJECTED-proposal defect, and every item in section 34.

## 7. Architecture

| Component | Frozen responsibility | Must not |
| --- | --- | --- |
| Today UI (`TodayView`) | Obtain the browser IANA timezone; request the projection; render sections; call existing action endpoints; explicit Calendar load. | Persist anything; call Calendar events automatically; add new mutation calls. |
| Today route (`app/api/today.py`) | Validate the request with fixed safe errors; resolve Clock and DB session; return the projection with `Cache-Control: no-store`. | Accept a body; accept any query parameter other than `timezone`. |
| Today service (`app/services/today_service.py`) | Compute the day window; run read-only SELECTs per section; isolate section failures. | Write, flush or commit; import or call any Calendar, LLM, Memory, Chat, RAG or Execution-tool module; own any domain write. |
| Today schemas (`app/schemas/today.py`) | The response contract in section 17. | Expose fields outside section 17. |
| Source domains | Remain the only owners of their state and writes. | Be modified for this phase. |

The Today service reads the ORM models `Task`, `Reminder`, `Notification`, `Execution`, `ExecutionStep` directly. It does not call domain service functions that write. No existing service, model, schema or route in those domains is edited.

## 8. Today Data Flow

1. The frontend reads `Intl.DateTimeFormat().resolvedOptions().timeZone`. If it is missing or empty, the UI shows the timezone error state (section 20) and sends no request.
2. On tab mount the frontend sends, in parallel: `GET /api/today?timezone=<IANA>` and `GET /api/calendar/connection`. Neither reaches Microsoft Graph.
3. The backend validates `timezone`, takes `now` from the injected Clock, and derives `local_date`, `day_start` and `day_end` (section 9).
4. The backend runs the four section queries in a fixed order (tasks, reminders, notifications, executions), each isolated (section 18), inside one request session that only reads.
5. The backend returns the projection. Nothing is stored.
6. The frontend renders. Calendar shows connection state only.
7. If the user clicks "加载今日日程", the frontend calls the existing `GET /api/calendar/events` with `start=day_start` and `end=day_end` taken verbatim from the Today response. Events live only in component state.
8. After any successful existing action, and when the user clicks "刷新", the frontend repeats step 2's Today request. Loaded calendar events are discarded on every Today refresh.

## 9. Timezone / Day-Boundary Contract

- "Today" is defined by the browser timezone, sent explicitly as the `timezone` query parameter. There is no server-local fallback and no default.
- Valid `timezone`: a string of 1–64 characters that is a member of `zoneinfo.available_timezones()` (installed tzdata). Anything else — missing, empty, repeated, unknown, a UTC offset such as `+08:00`, a path-like value — is rejected with the fixed 422 of section 16.
- `now` = the injected Clock's current UTC instant.
- `local_date` = `now` converted to the zone, calendar date part.
- `day_start` = the zone-aware datetime at `local_date` 00:00:00 (fold = 0), then converted to UTC.
- `day_end` = the zone-aware datetime at (`local_date` + 1 calendar day) 00:00:00 (fold = 0), then converted to UTC.
- The window is `day_start` inclusive to `day_end` exclusive.
- **Forbidden:** computing `day_end` as `day_start` + 24 hours, or adding any fixed duration to an instant to obtain a boundary. The "+ 1 day" above is calendar-date arithmetic on a date value. A DST day is therefore 23 or 25 hours long.
- If local midnight does not exist on a date (the zone skips it), the fold = 0 construction yields the first instant of that local date; that instant is the boundary. If local midnight occurs twice, the first occurrence is the boundary.
- `lookback_start` (used only in sections 11 and 13) = the zone-aware datetime at (`local_date` − 6 calendar days) 00:00:00 (fold = 0), converted to UTC. Together with today this covers seven local dates.
- Only after these aware instants are built are they converted for comparison, exclusively through `coerce_utc_for_db(value, DIALECT)`. Persisted datetimes are never reinterpreted, shifted or rewritten.
- The response returns `day_start`, `day_end` and `generated_at` as UTC ISO 8601 with `Z`, and `local_date` as `YYYY-MM-DD`.
- Verified at specification time with the installed tzdata 2026.5: America/New_York 2026-03-08 is 23 h, 2026-11-01 is 25 h; America/Havana 2026-03-08 has no local midnight and starts at 01:00 local.

## 10. Task Projection

Every task appears in at most one Today task list. Evaluation order:

| List | Eligibility (exact) | Order | Limit |
| --- | --- | --- | --- |
| `drafts` | `status == DRAFT` (any `due_at`, including null and past) | `due_at` ascending with nulls last, then `id` ascending | 50 |
| `overdue` | `status == CONFIRMED` and `due_at` is not null and `due_at < day_start` | `due_at` ascending, then `id` ascending | 50 |
| `due_today` | `status == CONFIRMED` and `day_start <= due_at < day_end` | `due_at` ascending, then `id` ascending | 50 |

- Excluded from all lists: `status == DONE`; `CONFIRMED` with `due_at >= day_end`; `CONFIRMED` with null `due_at`.
- The two excluded open categories are reported as counts only: `other_open.later` (CONFIRMED, `due_at >= day_end`) and `other_open.no_due_date` (CONFIRMED, null `due_at`).
- Postponed tasks need no special rule: postpone only changes `due_at`, and the task is re-evaluated on its current `due_at`.
- A draft with a past `due_at` is a draft, not overdue: it is not yet a commitment.
- No priority is invented. No other task status exists.
- Each list reports `total` (all eligible rows) and `truncated` (`total` > limit).

## 11. Reminder Projection

Read-only. No Reminder row, claim, lease, attempt count or status is touched. Each reminder appears in at most one list.

| List | Eligibility (exact) | Order | Limit |
| --- | --- | --- | --- |
| `scheduled` | `status` in (PENDING, CLAIMED) and `day_start <= remind_at < day_end` | `remind_at` ascending, then `id` ascending | 50 |
| `delivered` | `status == DELIVERED` and `day_start <= remind_at < day_end` | `remind_at` ascending, then `id` ascending | 50 |
| `attention` | (`status` in (PENDING, CLAIMED) and `remind_at < day_start`) or (`status == FAILED` and `lookback_start <= remind_at < day_end`) | `remind_at` ascending, then `id` ascending | 50 |

- CANCELLED reminders are excluded everywhere. DELIVERED or FAILED reminders outside the stated windows are excluded.
- A PENDING reminder whose `remind_at` is earlier today but not yet delivered stays in `scheduled`; the UI labels each row by its actual status and time, never as "will be delivered".
- Each row carries the owning task's current title, read from the Task domain at request time.
- `scheduled` must never be presented as a delivery guarantee. The section always shows the disclosure in section 20.
- No reminder action exists and none is added.

## 12. Notification Projection

- `unread`: every Notification with `read_at IS NULL`, regardless of date. Order: `created_at` descending, then `id` descending. Limit 20. `total` = count of all unread; `truncated` = `total` > 20.
- Read notifications are not shown.
- The only action is the existing `POST /api/notifications/{id}/read`.
- No new notification type and no delivery channel.

## 13. Execution Projection

`attention` lists executions with `owner_id == DEFAULT_OWNER_ID` and `execution_type` in `PUBLIC_EXECUTION_TYPES` that satisfy exactly one of:

| `reason` | Eligibility (exact) |
| --- | --- |
| `confirmation_required` | `status` not in the terminal set, and at least one of its steps has `requires_confirmation == true` and `confirmation_status` in (PENDING, EXPIRED) |
| `failed` | `status == FAILED`, and `lookback_start <= COALESCE(finished_at, updated_at) < day_end`, and no other execution has `retry_of_id` equal to this execution's id |
| `timed_out` | `status == TIMED_OUT`, with the same time window and the same no-retry-child condition |

- Order: `confirmation_required` first, then `failed`, then `timed_out`; within a reason `COALESCE(finished_at, updated_at)` descending, then `id` descending. Limit 20, with `total` and `truncated`.
- Excluded: SUCCEEDED, CANCELLED; CREATED / QUEUED / CLAIMED / RUNNING / CANCEL_REQUESTED without a pending or expired confirmation; internal types (`action.confirm`); failures already superseded by a retry.
- Today exposes no attempt, step, event, payload, hash, lease, idempotency key, error code or error message. The only action is navigation to the existing Executions tab for that execution.
- Confirmation is not granted or rejected from Today.

## 14. Calendar Contract

- Opening Today makes ZERO Microsoft Graph calls. `GET /api/today` never imports, constructs or calls the Calendar provider or auth objects.
- On mount the frontend calls only `GET /api/calendar/connection` (local token-cache read, no Graph).
- Calendar section states, driven only by that response and by the explicit load:

| State | Condition | UI |
| --- | --- | --- |
| Not configured | `configured == false` | "尚未配置 Outlook 日历。" No load button. |
| Disconnected | `configured == true`, `connected == false` | "Outlook 未连接。可在「日历」页连接。" No load button. |
| Ready | `connected == true`, nothing loaded | Button "加载今日日程". |
| Loading | after click | "正在读取今日日程…", button disabled. |
| Loaded | events returned | Event list, or "今天没有日历事件。" |
| Error | connection or events request failed | The safe error message from the existing error envelope; button "重试加载" (a new explicit click). |

- The load uses the existing `GET /api/calendar/events` with `start = day_start` and `end = day_end` from the current Today response. `GET /api/calendar/default` is not called.
- No automatic retry, no polling, no prefetch, no load on refresh. Every Graph-reaching request on Today is the direct result of one user click.
- Events are held only in component memory and discarded on Today refresh, tab change and reload. Nothing is cached or persisted on either side.
- Events are shown exactly as the existing projection provides them (title, time, all-day, cancelled, `show_as`, location). No free-time inference, no availability claim, no scheduling.
- A Calendar failure never hides or invalidates any local section.
- No Calendar write path is added or changed. The existing Calendar tab is unchanged.

## 15. Source-of-Truth Boundaries

Tasks → Task domain. Reminders → Reminder domain. Notifications → Notification domain. Executions → Execution domain. Calendar → Microsoft Calendar through the existing integration.
Today holds no copy of any of them between requests: no table, no cache, no summary, no planning object, no follow-up state, no user preference.
Every Today mutation is an existing domain endpoint called unchanged; Today owns no write.

## 16. API Contract

`GET /api/today?timezone=<IANA>` — the only new endpoint.

- No request body. No LLM. No external network. No write. No background task.
- Exactly one query parameter, `timezone`, exactly once. A missing, empty, repeated or invalid `timezone`, or any other query parameter, returns:
  - HTTP 422, body exactly `{"error":{"code":"VALIDATION_ERROR","message":"Invalid Today request"}}`.
  - The rejected value and any parameter name are never echoed. Validation is performed by the route itself so that the global validation formatter (which includes field locations) is not used for this endpoint. Validation behavior and error formatting of every other resource are unchanged.
- Success: HTTP 200 with the schema in section 17.
- Every response from this endpoint — 200, 422, 503, 500 — carries `Cache-Control: no-store`.
- Other statuses: 503 `SERVICE_UNAVAILABLE` (section 18); 500 `INTERNAL_ERROR` through the existing handler for unexpected errors.
- Other methods on the path are not provided.
- The endpoint is deterministic: for a fixed Clock, timezone and database state, two calls return identical bodies.

## 17. Response Schema

All datetimes are UTC ISO 8601 ending in `Z`. Field sets are exact; no other field is returned.

```
TodayResponse
  timezone: string            # the validated IANA name, as sent
  local_date: "YYYY-MM-DD"
  generated_at, day_start, day_end: datetime
  tasks: TaskSection
  reminders: ReminderSection
  notifications: NotificationSection
  executions: ExecutionSection

TaskSection
  status: "ok" | "unavailable"
  drafts, overdue, due_today: TaskList | null
  other_open: { later: int, no_due_date: int } | null
TaskList        { items: TodayTask[], total: int, truncated: bool }
TodayTask       { id: int, title: string, status: "DRAFT" | "CONFIRMED", due_at: datetime | null }

ReminderSection
  status: "ok" | "unavailable"
  scheduled, delivered, attention: ReminderList | null
ReminderList    { items: TodayReminder[], total: int, truncated: bool }
TodayReminder   { id: int, task_id: int, task_title: string, status: "PENDING" | "CLAIMED" | "DELIVERED" | "FAILED", remind_at: datetime }

NotificationSection
  status: "ok" | "unavailable"
  unread: NotificationList | null
NotificationList { items: TodayNotification[], total: int, truncated: bool }
TodayNotification { id: int, task_id: int, title: string, body: string, created_at: datetime }

ExecutionSection
  status: "ok" | "unavailable"
  attention: ExecutionList | null
ExecutionList   { items: TodayExecution[], total: int, truncated: bool }
TodayExecution  { id: int, execution_type: string, status: string, reason: "confirmation_required" | "failed" | "timed_out", occurred_at: datetime }
```

- `occurred_at` = `COALESCE(finished_at, updated_at)`.
- When a section's `status` is `unavailable`, every other field of that section is `null` — never an empty list and never zero.
- Task description, reminder lease/attempt/error fields, execution payload/error/attempt fields and all Calendar data are not part of this response.

## 18. Failure / Partial-Availability Model

- The four sections are computed independently. A `SQLAlchemyError` raised while computing one section rolls back the read session, marks that section `unavailable` (all other fields `null`), and computation continues with the next section.
- HTTP 200 is returned when the request is valid and at least one section is `ok`.
- If all four sections are `unavailable`, the endpoint returns HTTP 503 with the existing envelope `{"error":{"code":"SERVICE_UNAVAILABLE","message":"Database is unavailable"}}`. An all-empty page is never presented as success.
- A failure to obtain the database session before any section runs follows the existing error handling (503 for `OperationalError`).
- Non-database exceptions are not swallowed: they propagate to the existing 500 handler. Programmer errors are never converted into an `unavailable` section.
- An `ok` section with no rows returns empty lists and zero totals; `unavailable` is never used for "no data", and empty lists are never used for "could not read".
- No retry inside the endpoint.
- Frontend: an `unavailable` section renders "暂时无法读取此部分。" in place, with the other sections intact. A failed Today request (network, 503, 500) renders one page-level error with a "重试" button and no stale data. Calendar failures are confined to the Calendar section.

## 19. Existing Action Reuse

| Today control | Shown for | Existing endpoint (unchanged) |
| --- | --- | --- |
| 确认 | each `drafts` item | `POST /api/tasks/{id}/confirm` |
| 完成 | each `overdue` and `due_today` item | `POST /api/tasks/{id}/complete` |
| 延期 | each `overdue` and `due_today` item | `PATCH /api/tasks/{id}/postpone` with a timezone-aware `due_at` built by the existing `localDatetimeToIso` helper |
| 标记已读 | each `unread` notification | `POST /api/notifications/{id}/read` |
| 查看执行 | each execution item | navigation to the existing Executions tab (`?tab=executions&execution=<id>`) |
| 加载今日日程 / 重试加载 | Calendar section | `GET /api/calendar/events` |

- No Today mutation endpoint exists. Today reproduces no domain write logic and bypasses no confirmation boundary; domain errors (for example 409 `INVALID_TRANSITION`) are shown using the existing error envelope.
- While one action is in flight its control is disabled. After a successful action the Today projection is requested again. After a failed action the error is shown and the projection is also requested again.
- Confirming a draft keeps its existing meaning: DRAFT → CONFIRMED, creating a reminder when the task has a due time. Today adds no confirmation step and removes none.

## 20. Frontend / UX

- New tab "今天" is the first tab and the default landing tab. `initialTab()` returns `today` when the `tab` query parameter is absent or unrecognized. The Tasks tab becomes addressable as `?tab=tasks`. Existing `tab`, `execution` and `calendar_execution` URL behavior for all other tabs is preserved; the string `calendar_execution` remains in `App.tsx`.
- Header: "今天 · <local_date> · <timezone>" and a "刷新" button.
- Section order, fixed: 1. 逾期任务, 2. 今日到期, 3. 待确认草稿, 4. 今日提醒, 5. 未读通知, 6. 需要关注的执行, 7. 今日日程 (Outlook).
- Under the task sections: "另有 <later> 个之后到期、<no_due_date> 个未设截止时间的任务，可在「任务」页查看。" when either count is non-zero.
- Reminder section sub-groups in order: 需要关注, 今日待提醒, 今日已提醒. It always shows: "提醒由独立运行的提醒程序投递；当前版本无法检测它是否正在运行。"
- Loading: one page-level "正在加载…" for the Today request; the Calendar section has its own independent state.
- Empty states (section `ok`, nothing to show), fixed text: 逾期任务 "没有逾期任务。"; 今日到期 "今天没有到期任务。"; 待确认草稿 "没有待确认的草稿。"; 今日提醒 "今天没有提醒。"; 未读通知 "没有未读通知。"; 需要关注的执行 "没有需要关注的执行。".
- Truncation: when `truncated` is true the list shows "仅显示前 <n> 项，共 <total> 项。".
- Timezone error state: when the browser timezone cannot be determined, show "无法确定浏览器时区，无法显示今天的内容。" and send no Today request. No fallback zone is used.
- Refresh: on mount, on "刷新", and after each action. No polling, no timers, no automatic day rollover. A request-sequence guard discards a stale response that returns after a newer request or after the tab is left.
- No `localStorage`, `sessionStorage`, cookie or new URL parameter is written by Today.
- All text is rendered as escaped React text. No new frontend dependency. Styling reuses existing classes; new CSS is limited to additive `.today-` selectors.
- Today is a user-facing dashboard: no IDs as primary labels except where needed for the execution link, no engineering fields.

## 21. Privacy

- No LLM or provider transmission. No Memory, conversation or transcript access. No new telemetry.
- Opening Today sends nothing to any external service. The only external request reachable from Today is the already-existing Microsoft Calendar read, and only after an explicit click.
- Today responses contain task titles and notification text and are therefore `Cache-Control: no-store`.
- No new data leaves the machine and no new data is stored.

## 22. Logging

- Allowed new events, fixed format only: `today.projection timezone_valid=true sections_ok=<n> sections_unavailable=<n>` and `today.section_unavailable section=<tasks|reminders|notifications|executions> error=<exception class name>`.
- Never logged: the timezone value, task titles, notification text, any row content, row IDs, SQL text or parameters, raw exception `str`/`repr`, request URLs with query strings.
- `logger.exception` is not used for the new handling.
- No new frontend console logging.

## 23. Data Model / Migration Decision

Migration required: NO. No new table, column, index or constraint. The real database stays at b10c0d1e2f3a.
If delivering any requirement of this file would need persistence, STOP and return to architecture review. Do not create a migration.

## 24. Implementation Scope

Claude is the only AI authorized to modify product code, and only after the acknowledgement is reviewed and implementation is explicitly authorized.
Backend: one route module, one read service, one schema module, router registration.
Frontend: one new component, additive types and API functions, tab registration and default-tab change, additive `.today-` styles.
Tests: new focused suites; no existing test is expected to change.
No refactor of any domain service. No dependency change.

## 25. Allowed Files / Components

Maximum implementation allowlist:
- backend/app/api/today.py (new).
- backend/app/services/today_service.py (new).
- backend/app/schemas/today.py (new).
- backend/app/main.py — ONLY registration of the Today router.
- frontend/src/components/today/TodayView.tsx (new).
- frontend/src/App.tsx — ONLY the Today tab, tab order, default tab and `?tab=tasks` addressing.
- frontend/src/types.ts and frontend/src/api.ts — additive Today types and the Today request function only.
- frontend/src/index.css — additive `.today-` selectors only; no existing rule is edited.
- backend/tests/test_phase11a_today_boundaries.py, test_phase11a_today_projection.py, test_phase11a_today_api.py, test_phase11a_today_isolation.py (new).

No existing test file is on the allowlist. The specification pre-scan found no existing test that this phase must change. If one fails because of an approved Phase 11A change, STOP and request a recorded allowlist amendment; do not edit it first.
No existing backend service, model, schema, route (other than `main.py` registration), migration, or frontend component other than `App.tsx` may be edited.
During this specification task, ONLY docs/specs/phase11a-today-frozen-spec.md may be created or updated.

## 26. Prohibited Changes

No real DB mutation or migration. No real provider call. No Microsoft Graph call in tests or implementation work. No OAuth flow.
No `.env`, `.env.example`, `.gitignore`, dependency manifest or lockfile change; no new package.
No change to Task, Reminder, Notification, Execution, Calendar, Memory, Chat or RAG behavior, statuses, state machines, idempotency or retry rules.
No worker change, heartbeat, supervision or liveness inference.
No handoff or README edit during implementation. No edit to historical migrations.
No push, reset, checkout, rebase or cleanup of unrelated files. No intermediate commit.

## 27. Claude Spec Acknowledgement Gate

Claude reads the full handoff, this entire file and the relevant current code, then outputs one report headed `PHASE 11A SPEC ACKNOWLEDGEMENT` covering: understood goal; exact scope and non-goals; the day-boundary contract in its own words including the DST and no-midnight cases; exact eligibility for every list; the Calendar zero-Graph rule; the failure model; existing-action reuse; the allowlist; the test gate; the gate sequence; and every concrete ambiguity or conflict found between this file, the code and the tests.
It must include a scan of existing tests for assumptions this phase would break (default tab, route inventory, source-text checks).
STOP after the acknowledgement. No code, test or other file change until it is reviewed and implementation is authorized.

## 28. Claude First-Pass Test Gate

Offline only: fake/injected Clock, disposable file-backed SQLite, no real provider, no Graph, no real DB. Test DB and storage environment set before app imports.

1. **Timezone validation.** Valid zones accepted (UTC, Asia/Shanghai, America/New_York, Europe/London, Asia/Kathmandu, Australia/Lord_Howe, Pacific/Kiritimati, Pacific/Pago_Pago). Missing, empty, repeated, unknown, offset-style and path-like values, and any extra query parameter, return the exact fixed 422 with `no-store`; canary values and parameter names never appear in the response; an unrelated resource's 422 format is unchanged.
2. **Day boundaries.** For each zone: `local_date`, `day_start`, `day_end` for instants just before, exactly at and just after local midnight. A spring-forward day is 23 h and a fall-back day 25 h, computed against installed tzdata. A zone whose transition removes local midnight starts at the first instant of the date. A test that would pass under `start + 24h` must fail under this contract (a row placed in the 24th–25th hour of a fall-back day is today; a row in the hour after a 23-hour day ends is not).
3. **Tasks.** Overdue, due today, tomorrow, DONE, no due date, DRAFT with past/today/future/null due date; rows exactly at `day_start` and `day_end`; single-list membership; `other_open` counts; ordering; limit, `total`, `truncated`; SQLite naive-UTC storage read correctly.
4. **Reminders.** Every status against every window; `attention` rules including the lookback edge; CANCELLED exclusion; task title join; zero Reminder mutation (rows, statuses, attempt counts, leases identical before and after).
5. **Notifications.** Unread only; ordering; limit; totals.
6. **Executions.** Each reason; non-terminal without pending confirmation excluded; SUCCEEDED/CANCELLED excluded; internal type excluded; other owner excluded; retry-superseded failures excluded; lookback edge; ordering; no engineering field in the response.
7. **Schema.** Exact key sets at every level; `unavailable` sections are all-null; determinism (two calls, identical bodies under a fixed Clock).
8. **No writes.** Statement capture shows only SELECT (and PRAGMA) during the request; database file bytes are identical before and after; all domain tables and sequences are identical.
9. **No external access.** The Calendar provider/auth factories, the LLM provider factory and the Memory selector are replaced by objects that raise if touched; the request still succeeds. A socket guard records zero non-loopback connections.
10. **Isolation.** Each section forced to fail in turn: that section is `unavailable`, the others are correct, HTTP 200. All four failing: 503. A non-database exception: 500, not `unavailable`. Logs contain only the fixed events, with no row content, timezone value or SQL.
11. **Regression.** Full backend suite on isolated DBs with actual counts; no hardcoded expected total.
12. **Frontend.** `npm run build`, `npm run lint`. Bounded browser QA on a disposable migrated backend and loopback frontend, with the Calendar client ID unset in the QA process so no token-cache path is touched: Today is the default tab; `?tab=tasks` and all existing tab URLs still work; every section's loading, empty, populated, truncated and unavailable state; the timezone error state; each existing action calls its existing endpoint and Today refreshes; a failed action shows the domain error; no `/api/calendar/events` or `/api/calendar/default` request before the click; exactly one events request per click with `start`/`end` equal to `day_start`/`day_end`; Calendar disconnected and Calendar failure states leave local sections intact; nothing written to browser storage; stale-response guard.

Record commands, versions, results, the diff identity, the allowlist check and protected-state fingerprints before and after. A failure pauses the gate.

## 29. Luna Independent QA Gate

Luna reads, tests and reports; no product or test-source edits. Independent adversarial probes, not a rerun of Claude's tests:
- DST and day boundaries in zones Claude did not use, including a no-midnight transition and half-hour offsets.
- Proof of no writes (bytes, rows, sequences) across every Today path.
- Proof of zero Graph and zero provider calls on Today load, and exactly one Graph-reaching request per explicit click.
- Section isolation under injected failures.
- Source-of-truth preservation: every value on Today matches the owning domain's own API at the same instant.
- Validation privacy of the 422.
Evidence is tied to the exact reviewed diff and environment.

## 30. Architecture Review Gate

The Frozen Spec Owner reviews the route, service, schemas, frontend and tests against this file. Acceptance requires: no persistence, no write, no Graph on load, the exact day-boundary construction, exact eligibility rules, and no scope growth. Deviations are resolved by a recorded amendment, never by implementation discretion.
High-cost architecture review is not required unless a genuine architecture conflict appears.

## 31. Real Provider Gate Decision

NOT REQUIRED. Phase 11A uses no LLM and changes no provider semantics. No real provider call is authorized.

## 32. Real DB Gate Decision

NOT REQUIRED. No migration is allowed. The real database must not be modified during implementation or QA. Compatibility with the real schema is shown on disposable databases migrated to b10c0d1e2f3a by the normal Alembic path.

## 33. Exit Criteria

- Acknowledgement reviewed; implementation explicitly authorized.
- Implementation inside the allowlist; first-pass gate PASS on the final source.
- Luna independent QA PASS against the final diff.
- Frozen Spec Owner implementation review PASS.
- Final acceptance, then a separately authorized final checkpoint commit. No push.
- No Real Provider Gate and no Real DB Gate.

## 34. Deferred Debt

Unchanged and not addressed here: worker observability and auto-start; reminder follow-up (snooze, re-arm after delivery, failed-reminder handling); planning; Memory consumption beyond ordinary Chat; execution-result feedback into Chat; RAG/Chat integration; Calendar caching or free/busy reasoning; real expired-token refresh confidence; the minimal datetime parser used by action proposals; the pre-existing ChatView REJECTED-proposal defect; dependency locking; legacy logging suppression mismatch; httpcore2 response-header residual; single-process proposal store; live PostgreSQL coverage; unrelated .gitignore / apisix-docker state.
New, accepted for this phase: Today does not update itself (no polling or day rollover) and shows only the current local day.

## 35. Implementation Order

1. Day-window computation and timezone validation, with boundary tests.
2. Today service section queries, with projection tests.
3. Schemas, route, registration; API, no-write, no-external and isolation tests.
4. Frontend types and API function; `TodayView`; tab registration and default.
5. Frontend build, lint, browser QA.
6. Full backend regression.
No intermediate commit.

## 36. Stop / Escalation Rules

STOP and report to the Frozen Spec Owner, without deciding locally, if:
- The day-boundary contract cannot be met without redefining stored datetime semantics.
- Any requirement needs persistence, a migration, a new dependency, a second endpoint or an out-of-allowlist edit.
- Calendar cannot stay explicit and on-demand without changing the existing Calendar integration.
- An existing action cannot be reused without duplicating domain write logic.
- Any Today path would write, call Graph automatically, call a provider, or read Memory or conversations.
- Honest reminder presentation would require worker-status persistence.
- An existing test fails because of an approved change.
- A source-of-truth conflict or an eligibility rule that the current models cannot express is found.
Report the exact evidence and a narrow proposed correction. Do not weaken a rule to continue.

## 37. Git / Commit Rules

Specification task: create or update ONLY this file; no stage, commit or push.
Implementation and QA: no stage, commit or push before final acceptance and a separate checkpoint authorization; no intermediate commits.
Never stage .gitignore, apisix-docker/, backend/jarvis.db, .env files, node_modules, build output or external evidence.
At the final checkpoint, stage only explicitly reviewed Phase 11A files (including this specification), verify the staged set, create one normal commit, and verify post-commit state. No amend, push, pull, fetch, reset, checkout or rebase.
End of specification task: CLAUDE SPEC ACKNOWLEDGEMENT REQUIRED — NO CODE CHANGES YET.
