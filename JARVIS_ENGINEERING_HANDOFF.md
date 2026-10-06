# JARVIS Engineering Handoff

> **CURRENT ENGINEERING HANDOFF** — 当前状态快照，不是历史日志。
> 新会话（Claude Code / Luna QA / Astra Architect）应先读本文件，再按需阅读源码。
> 本文件与源码冲突时，以源码与 Alembic 迁移链为准，并报告差异。

---

## 1. Project Identity

| 项 | 值 |
| --- | --- |
| Project | Personal AI Assistant（JARVIS） |
| Repository | `C:\Users\25795\Desktop\Personal AI Assistant（JARVIS）` |
| Branch | `main` |
| Accepted baseline commit | `5b8d343dba31fde13c4c29018492e77675e7b2ea`（`feat: add minimal personal memory foundation`，2026-09-26） |
| Phase 10B implementation checkpoint | `9040c83d9019e3a1d51000e4d0d7e8320f8c530a`（`feat: add explicit memory proposal and confirmation checkpoint`，2026-10-03） |
| Phase 10B final checkpoint | `9040c83` 之上的下一个 commit：`chore: close phase 10b memory proposal validation`（2026-10-06）：`1a9de59eb6060579b80592262eb7afc681fc97c3` |
| Phase 10C final checkpoint | `1a9de59` 之上的下一个 commit：`feat: complete phase 10c selective memory context`（2026-10-06；SHA 以 `git log` 为准，文件无法自引用）。Phase 10C 验收后，该 commit 为当前 accepted baseline |
| Current Python | 3.12.10（`backend\.venv`） |
| Stack | Backend FastAPI + SQLAlchemy + Alembic（SQLite 默认 / PostgreSQL 可选）；Frontend React + TypeScript + Vite |

运行边界：

- **single-user**
- **local-first**
- **Windows-first**

当前**不是** public multi-user production service：没有认证（Authentication）与用户隔离，不得直接部署到公网。

---

## 2. Official Phase State

| Phase | 状态 |
| --- | --- |
| Phase 0–9 | COMPLETE / CLOSED |
| Phase 9.5 — DB Lifecycle Hardening | COMPLETE / CLOSED |
| Phase 10A — Minimal Personal Memory Foundation | COMPLETE / CLOSED |
| Phase 10B — Explicit Memory Proposal & Confirmation | **CLOSED / ACCEPTED**（Astra，2026-10-06） |
| Phase 10C — Explicit Per-Request Personal Memory Use in Ordinary Chat | **CLOSED / ACCEPTED**（2026-10-06） |

- Current open development phase: **无**。下一阶段尚未规划，也未授权（见 §19）。
- Phase 10C 规格与验收记录：[`docs/specs/phase10c-frozen-spec.md`](docs/specs/phase10c-frozen-spec.md)
- Phase 10C final gate state：
  - Claude implementation / First-Pass Gate：**PASS**
  - Luna Independent QA：**PASS**
  - Architecture / Implementation Review：**PASS**
  - Real Provider Gate：**NOT REQUIRED**
  - Real DB Gate：**PASS**（真实库 `ae1f2a3b4c5d` → `b10c0d1e2f3a`，见 §7）
- Phase 10B 规格与验收记录：[`docs/phase10b-memory-proposals.md`](docs/phase10b-memory-proposals.md)
- Phase 10B final gate state：
  - Implementation / Regression：**PASS**
  - SDK 3.24 environment / privacy：**PASS**
  - Real Provider Gate：**PASS**（2026-10-05；deepseek / `deepseek-v4-flash`；授权 2 次真实业务调用，实际 2 次；SDK transport 重试 0 次）
  - Real DB Gate：**PASS**（当时迁移到 `ae1f2a3b4c5d`；真实库此后已由 Phase 10C 迁移到 `b10c0d1e2f3a`，见 §7）

---

## 3. Current Product Capabilities（当前 contract）

| 能力 | 当前 contract 摘要 | 主要入口 |
| --- | --- | --- |
| Task | 状态机 `DRAFT → CONFIRMED → DONE`；`CONFIRMED` 可 postpone；非法转换 409；`due_at` 必须带时区。Action Confirm 创建的 Task 仍为 `DRAFT`（见 §4） | `/api/tasks` |
| Reminder | 两个创建来源：① Task domain confirm（`DRAFT → CONFIRMED`）时若有 `due_at` 则创建 PENDING Reminder；② Phase 8 Action Confirm `create_task_with_reminder` 在同一事务中创建 DRAFT Task + PENDING Reminder。独立 Reminder Worker 轮询、原子领取、at-least-once + 唯一约束去重；应用内通知 | `/api/reminders`、`/api/notifications`；`python -m app.workers.reminder_worker` |
| Conversation | 对话与消息持久化；USER 先落库，模型失败不伪造 ASSISTANT | `/api/conversations` |
| LLM Gateway | `LLMProvider` 抽象；OpenAI / DeepSeek / Fake；Provider 无 DB 或外部副作用 | `app/llm/` |
| Document ingestion | txt/md/pdf（仅文本）/docx；扩展名白名单、magic bytes、ZIP bomb 防御；确定性切块 | `/api/documents` |
| Retrieval / RAG | SQLite：FTS5/BM25 + LocalHash（非语义）+ RRF；PG：ILIKE 回退 + pgvector；RAG 后端分配引用标签 + allowlist 校验，证据不足弃权 | `/api/retrieval`、`/api/rag/ask` |
| Execution Engine | Execution / Attempt / Step / Event、租约、重试、`run_at`、确认门 | `/api/executions`；`python -m app.services.execution_worker` |
| Outlook Calendar | Microsoft Graph 读（calendarView）+ 经 Proposal/Confirm/Execution 的写 | `/api/calendar`（仅本地请求） |
| Personal Memory | Phase 10A 手动 CRUD（见 §5）；Phase 10B：记忆 tab 内显式自然语言提案 → 预览 → 显式确认；Phase 10C：普通 Chat 可在单次发送中只读使用显式勾选的已保存偏好（见 §4 E、§5） | `/api/memories`、`/api/memories/proposals`；前端「记忆」tab；`POST /api/conversations/{id}/messages` 的可选 `memory_keys` |

健康检查：`/health`（liveness）、`/health/db`（readiness，只读核对 revision）、`/health/embedding`。

---

## 4. Real Action Flows（当前真实成立的链路）

**A. Task / Reminder**

```
Natural Language
→ Task / Reminder Proposal   (POST /api/conversations/{id}/action-proposals，只读，不写业务数据)
→ Preview
→ Explicit Confirm           (POST /api/conversations/{id}/action-proposals/confirm)
→ Task / Reminder domain     (同一事务原子写入；action.confirm 仅作内部幂等回执)
```

Action Confirm 写入结果：

- `create_task` → 创建 **DRAFT** Task
- `create_task_with_reminder` → 同一事务创建 **DRAFT** Task + **PENDING** Reminder

Action Confirm 的含义是「用户确认了创建该 action / 对象」，**不**表示 Task 状态已转换为 `CONFIRMED`。Task domain 仍保有自己的 `DRAFT → CONFIRMED` 语义（`POST /api/tasks/{id}/confirm`）。

另有早期链路 `POST /api/conversations/{id}/task-proposals` → DRAFT Task → `POST /api/tasks/{id}/confirm`（`DRAFT → CONFIRMED`；有 `due_at` 时同时创建 PENDING Reminder）。

**B. Calendar**

```
Natural Language
→ Calendar Proposal          (intent = create_calendar_event)
→ Preview
→ Explicit Confirm
→ Durable Execution          (calendar.create_event，Execution Worker 执行)
→ Microsoft Graph            (POST /me/calendar/events，稳定 transactionId)
→ Outlook Event
→ read-back                  (权威 GET 回读)
```

Action Proposal intent allowlist：`create_task`、`create_task_with_reminder`、`create_calendar_event`、`unsupported`。

**C. Personal Memory（Phase 10A）**

```
Manual Memory UI / API → Create / Read / Update / Delete
```

**D. Personal Memory（Phase 10B）**

```
记忆 tab 一句话 → 冻结 V1 语法（全匹配）→ 受约束 LLM candidate → strict schema
→ 确定性策略 → 精确 key 查询 → Preview（进程内存，10 分钟）
→ Explicit Confirm → Memory 变更 + receipt 单事务
```

仅记忆 tab 为入口；Chat / Action Proposal / RAG / Task / Calendar 不写 Memory。不使用 Execution、Worker 或 `action.confirm`。

**E. 普通 Chat 单次使用已保存偏好（Phase 10C）**

```
普通 Chat 勾选（每次发送，默认不勾选）→ USER 先落库 → 过滤后的历史
→ Memory 快照（独立短读会话，精确 key，严格校验）→ 固定 policy + 数据消息 + 历史
→ Provider（应用层至多 1 次生成）→ ASSISTANT 回复 + 来源标记 单事务
```

Phase 10C contract（详见 `docs/specs/phase10c-frozen-spec.md`）：

- 能力：普通 Chat 可在**单次请求**中使用用户**显式勾选**的已保存偏好。
- 选择：最多 3 个 canonical key（`preferred_study_time`、`preferred_session_duration`、`usual_available_days`）；仅本次发送有效；默认不勾选；不持久化（无全局开关、无 localStorage / sessionStorage / URL）。
- Memory 使用：**READ-ONLY**。`manual` 与 `user_confirmed` 同等可用；值必须是合法 canonical 值，否则整条省略（不强制转换、不修复）。
- Automatic extraction：**NO**；Automatic Memory mutation：**NO**；Vector / semantic Memory：**NO**；relevance LLM：**NO**。
- API：无新端点。`POST /api/conversations/{id}/messages` 新增可选 `memory_keys`；`MessageOut` 新增 `memory_context_applied`；`ChatExchangeOut` 新增 `memory_context`（`status` ∈ `disabled` / `applied` / `empty` / `unavailable`，`keys` 为实际提供的 key）。该 messages 资源的所有响应 `Cache-Control: no-store`，422 使用固定脱敏文案。
- 来源标记：`messages.memory_context_applied` 仅在「成功提供了非空偏好 payload 后生成的普通 Chat ASSISTANT 回复」上为 true；RAG / 任务建议等其他写入方始终为 false。
- **被标记的历史 Chat 回复在本地仍然可见（`GET messages` 返回完整记录），但被排除在之后所有自动 Provider 历史之外**——包括普通 Chat、任务建议与行动提案（统一经 `app/llm/messages.py`）。更新或删除 Memory 后同样如此；删除 Memory 不会删除历史回复。
- Fail-closed：只有来源标记被证明为合法存储的 false 的历史行才进入 Provider 历史；true / NULL / 损坏的原始值一律排除（ORM 解码出的 False 不足为证，另有数据库派生的有效性属性）。
- 预算：确定性的 byte-token accounting units（≤ 1024），**不是** Provider 原生 token。
- Real Provider Gate：NOT REQUIRED；Real DB Gate：PASS。

---

## 5. Memory Contract（Phase 10A 正式 contract）

详细规格：[`docs/phase10a-memory.md`](docs/phase10a-memory.md)。

- Categories：`preference`、`stable_fact`、`constraint`（仅此三类）
- Memory 性质：user-visible、editable、deletable、source-aware、version protected
- `source_type`：`manual`；Phase 10B 新增 `user_confirmed`（确认的提案）。服务端控制，客户端 / LLM 不可提供；手动更新总是重置为 `manual`
- value：bounded structured JSON（标量 / ≤64 标量列表 / ≤32 字段扁平对象；无 null、无嵌套；紧凑 JSON ≤ 4096 UTF-8 bytes）
- key：1–80 ASCII，小写字母开头，`[a-z0-9_]`；单用户内全局唯一（重复 409 `MEMORY_KEY_CONFLICT`）
- version：optimistic concurrency（`expected_version` 数据库谓词 CAS；陈旧 409 `MEMORY_VERSION_CONFLICT`；无自动重试、无静默覆盖）
  - 注：`expected_version` 冲突保护当前仅适用于 **UPDATE**；当前 **DELETE** 按 ID 删除，不要求 `expected_version`。
- delete：application-level hard delete（无历史、无 tombstone、无 undo；**不是**存储/备份层面的安全擦除）

明确**不存在**：

- NO automatic memory（Phase 10B 只有显式请求 + 显式确认；无静默写入）
- NO free-form LLM extraction（Phase 10B 只支持 3 个 canonical key 与冻结语法）
- NO automatic / implicit context injection（Phase 10C 只有显式、单次、只读的使用，见下）
- NO automatic Memory extraction or mutation from Chat（普通 Chat 对 Memory 只读）
- NO semantic / vector memory
- NO Planning integration

---

## 6. Source of Truth

| 数据 | 唯一事实来源 |
| --- | --- |
| Task state | Task domain |
| Reminder state | Reminder domain |
| Calendar event | Microsoft Calendar（Outlook / Graph） |
| Execution status | Execution domain |
| Documents / RAG | Document / Retrieval domain |
| Personal Memory | 相对稳定的偏好 / 背景事实 / 长期约束 |

Memory **不得**复制其它 domain 的 mutable operational truth（例如「任务 5 已完成」「会议改到下午 3 点」「提醒已发送」「execution 10 成功」）。Memory 不校验、也不同步其它 domain。

---

## 7. Database

- Production schema authority：**ALEMBIC ONLY**
- Production startup：NO `create_all`、NO automatic upgrade、NO automatic stamp、NO automatic repair
- Startup：**READ / VERIFY ONLY**（revision 必须等于迁移链唯一 head，否则 fail closed；SQLite 文件不存在时在首次连接前拒绝启动）
- Accepted head（Phase 10C）：**`b10c0d1e2f3a`**（仅新增 `messages.memory_context_applied`：Boolean、NOT NULL、server default false；down_revision `ae1f2a3b4c5d`；迁移链线性，共 15 个 revision）
- 此前的 accepted head：Phase 10B `ae1f2a3b4c5d`（`user_confirmed` source + `memory_confirmation_receipts`）；Phase 10A `9d0e1f2a3b4c`
- Current real SQLite DB：`backend/jarvis.db`
  - Alembic revision：**`b10c0d1e2f3a`**（Phase 10C Real DB Gate PASS，2026-10-06；由显式授权的 `alembic upgrade head` 从 `ae1f2a3b4c5d` 迁移。此前 Phase 10B gate 已从 `9d0e1f2a3b4c` 迁移到 `ae1f2a3b4c5d`）
  - 真实库现在**要求** Phase 10C 的代码与 schema：早于 Phase 10C final checkpoint 的源码指向该库会按设计 fail closed。
  - Phase 10C gate 结束时的已接受状态：`conversations` = 1，`messages` = 0，`foreign_key_check` clean，无 WAL / SHM / journal sidecar。
  - 当前预期的 Phase 10B 持久审计状态（Phase 10C 迁移未改变）：
    - `personal_memories` rows = **0**
    - `memory_confirmation_receipts` rows = **2**（`CREATE` 与 `UPDATE` 各一条，均为 `APPLIED`）
    - `personal_memories` 的 `sqlite_sequence`（AUTOINCREMENT high-water）= **2**
    - SQLite `quick_check`：`ok`
  - **这 2 条 receipt 是有意保留的历史审计记录**（Real DB Gate 的确认回执；receipt 无外键，按设计在 Memory 删除后继续存在）。**不得**仅为了让数据库「看起来干净」而删除它们；high-water = 2 同理不得重置。
  - Phase 10C Real DB Gate 结束时的 DB SHA-256（gate 时点证据，**不是**规格；此后任何合法写入都会改变它）：`20e29462c03cdfbf2af0c69bb2a51de82cfa40e4b13fd4d31d23705603a84c81`
    - 历史：Phase 10B Real DB Gate 结束时为 `6594741e55809557a7133dff5f9477cb0562bb3e4c9f4199c4e797bfa057c842`（即 Phase 10C 迁移前的哈希）。
  - Real DB Gate 的备份 / 证据目录均在仓库外，**不得**复制进仓库：
    - Phase 10C：`C:\Users\25795\JARVIS-DB-Gate-Backups\phase10c-20261006T035423Z\`（`gate-report.json`、迁移前备份 `jarvis-pre10c-20261006T035423Z.sqlite.bak`、`gate.py`、`alembic-upgrade.log`）
    - Phase 10B：`C:\Users\25795\JARVIS-DB-Gate-Backups\phase10b-20261005T101920Z`
  - 以上状态于 2026-10-06（Phase 10C final checkpoint 前）以只读方式（`mode=ro`）复核，与上述记录一致。这是最近验收状态，不是永久规格。
- 测试从不接触真实 `jarvis.db`：测试使用 disposable SQLite，并显式执行 `alembic upgrade head`；测试中的隔离 `create_all` 仅为脚手架，不属于 production startup。

---

## 8. Fresh Database Workflow

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
alembic upgrade head        # 先迁移
uvicorn app.main:app        # 再启动 JARVIS
```

- FastAPI startup **不得**代替 migration。
- 普通 schema / revision mismatch **不要**使用 `alembic stamp head` 作为正常修复；版本标签本身不能证明结构正确。
- revision readiness 不是完整物理 schema diff，也无法防御人为错误 stamp。

---

## 9. Microsoft Calendar

| 项 | 当前实现 |
| --- | --- |
| Provider | Microsoft Graph Global（`https://graph.microsoft.com/v1.0`，authority `https://login.microsoftonline.com/common`） |
| OAuth | Public Client，MSAL（`PublicClientApplication`），**no client secret** |
| Token persistence | Windows DPAPI（当前用户），存放于仓库外（`%LOCALAPPDATA%`）；无明文回退、不入数据库 |
| Permissions | `Calendars.Read`；写入另需显式授权 `Calendars.ReadWrite` |
| Read | `GET /me/calendar/calendarView`（有界投影） |
| Write | `POST /me/calendar/events` |
| Create reliability | 稳定 Graph `transactionId`（canonical UUID）+ 权威 GET read-back |
| API 访问 | `/api/calendar/*` 仅接受本地请求 |

真实 Personal Microsoft Account：read verified、write verified、real event verified。

重试分为三个**相互独立**的层，不能合并理解为「Calendar 请求总共最多重试 2 次」：

- **A. Transport bounded retry budget**：针对 429、部分 5xx、transport 错误；每次请求最多 2 次重试（适用时）+ 退避。
- **B. 401 处理**：401 可触发一次 token refresh / 重新获取，并额外重发一次；这与 A 的重试预算分开计算。
- **C. Execution Worker attempts**：Worker 级 retry / Attempt 生命周期（`calendar.create_event` `max_attempts=3`）是另一层。

**不要声称** wire-level HTTP POST exactly once：可靠性来自稳定 `transactionId` + 权威 read-back，而不是单次发送保证。一次 Worker Attempt **不能证明**只发出一次 wire-level HTTP POST。

---

## 10. LLM

- Supported providers：`openai`、`deepseek`、`fake`（`LLM_PROVIDER`）
- 当前本地配置（仅名称，来自 `.env` 非敏感键）：`LLM_PROVIDER=deepseek`，`LLM_MODEL=deepseek-v4-flash`
  - 代码默认值：`openai` / `gpt-4o-mini`
  - 本文件**不记录**任何 API key、token、secret。
- 结构化输出：OpenAI 使用 strict `json_schema`；DeepSeek 使用 `json_object`（JSON Mode）+ schema 指令；两者都经后端 Pydantic 二次验证。
- DeepSeek real-provider compatibility（commit `d95d6ab`）：
  - structured-output contract issue was fixed
  - datetime grounding preserved（当前日期/时区由程序注入）
  - Chinese absolute date support
  - deterministic canonicalization
  - real provider proposal verified
- 重试分层：
  - **APPLICATION LAYER**：JARVIS 应用代码没有额外实现生成型调用的重试循环。
  - **SDK / TRANSPORT LAYER**：OpenAI SDK 客户端构造时未设置 `max_retries=0`，DeepSeek 走同一客户端路径；已安装 SDK 的默认配置可能执行 transport 级重试。
  - 断言「SDK 方法只被调用一次」的 mock **不能证明**只发出一次 wire-level HTTP 请求；不得声称 wire-level exactly-once。
- 当前验证运行时（2026-10-05，`backend\.venv`）：
  - `openai` **3.24.0**（Astra 接受为当前 validation runtime；不降级，不重建已丢失的历史 SDK 版本）
  - 默认 HTTP 栈：`openai 3.24.0 → httpx2 2.13.1 → httpcore2 2.13.1`（不再是 httpx / httpcore）
  - legacy `httpx 0.28.1` / `httpcore 1.0.9` 仍安装：Graph 客户端使用；SDK 也仍接受显式注入的 legacy `httpx.Client`
  - `python-multipart 0.0.32`（Documents API 上传所需的运行期依赖，已在 `requirements.txt` 声明）
  - `pypdf 6.19.0`、`python-docx 1.2.0`（文档解析所需的运行期依赖，已在 `requirements.txt` 声明）
- SDK / transport 日志（详见 `docs/phase10b-memory-proposals.md` R2）：
  - openai 3.24.0 的 `openai.*` 日志仅含元数据；`app/llm/sdk_logging.py` 为纵深防御（历史 SDK 版本会在 DEBUG 记录请求体）。
  - `httpx2` / `httpcore2` logger **未**过滤；Phase 9A `secure_library_logging()` 只压制 legacy `httpx` / `httpcore`。
  - **已接受残留（Phase 10B）**：httpcore2 在 DEBUG 记录完整响应头（可能含 request id、限流元数据、cookie、供应商字段）。原始 DEBUG 日志**不得**作为可公开 / 可分享证据。

---

## 11. Execution

组成：Execution、Attempt、Step、Event、Lease（CAS 原子 claim + 租约恢复）、Retry（有界 + 确定性退避）、`run_at`（一次性调度，naive 时间拒绝，misfire 策略）、Confirmation Gate（步骤级 grant/reject，显式 actor）。

Public registry（`PUBLIC_EXECUTION_TYPES`）：

- `system.ping`
- `reminder.send`
- `documents.reindex`（需确认）
- `calendar.create_event`（`api_creatable=False`）

Internal（`INTERNAL_EXECUTION_TYPES`，公共 API 视为不存在 → 404）：

- `action.confirm`（Phase 8B 确认回执；同步完成，不排队、不派发工具）

**特别注明**：`calendar.create_event` **不能**通过普通 public Execution create API（`POST /api/executions`）创建，从而不能绕过 Proposal / Confirm。它只能由 Action Proposal 确认链路创建。

---

## 12. Security Invariants（必须保持）

- LLM cannot directly execute side effects（Provider 只做「消息 → 文本 + 元数据」）
- important writes use controlled preview / confirm
- typed validation — 校验策略**按 schema 而定**，不是全局统一：
  - 安全敏感输入使用显式类型化校验；
  - Memory 输入在已实现处使用 `strict=True` + `extra="forbid"`；
  - Action Proposal 使用类型 / `Literal` / 字段校验与 `extra="forbid"`，但这**不等同于**全局 Pydantic `strict=True`；
  - 部分输出适配器有意采用不同的多余字段策略（例如 `RAGModelOutput` 为 `extra="ignore"`：多余字段被忽略且无权威性，缺字段仍失败）
- unknown-field rejection — 在 schema 声明 `extra="forbid"` 的位置（多数 action / memory / 请求输入）拒绝未知字段；并非所有模型都如此
- action / tool allowlists
- no arbitrary function execution（无 eval / exec / 反射 / 任意函数名）
- datetime program authority（当前时间与时区由程序决定，不信任模型）
- fail closed
- safe errors（不泄露堆栈、SQL、连接串、路径、原始模型输出）
- no token leakage
- bounded external retries
- idempotency where relevant
- Memory no silent write

---

## 13. Testing — LAST VERIFIED — 2026-09-23

以下为 Phase 10A 正式验收时的结果（对应已接受 baseline `5b8d343`）。**不是**每次启动都保证的状态；代码变更后必须重新验证。仓库内未保存可引用的报告 / 日志文件路径。

| 项 | 结果 |
| --- | --- |
| Full pytest | 1134 passed, 37 skipped, 1171 collected, 0 failed |
| Phase 10A tests | 63 PASS |
| Phase 10A migration tests | 3 PASS |
| Schema lifecycle tests | 13 PASS |
| Frontend lint | PASS |
| Frontend build | PASS |
| Real DB migration | PASS |
| Real Memory CRUD | PASS |
| Real PostgreSQL | NOT VERIFIED（本 baseline 未运行） |

Skipped 测试均为门控（真实 PostgreSQL / 真实外部服务），默认跳过而非伪造通过。

### Phase 10B — Claude first-pass（2026-09-29，含 Astra 裁定修正；当时为 `a6e1b8e` 上的未提交工作区，现为 checkpoint `9040c83`）

**历史记录**：这是实现者首轮结果，来自**旧环境**（旧版 OpenAI / FastAPI / Pydantic / SQLAlchemy / pytest），不能代表当前环境。下表中的 gate 状态是当时的状态；最终状态见本节末尾「Phase 10B — FINAL ACCEPTED」。

| 项 | 结果 |
| --- | --- |
| Full pytest | 1365 passed, 37 skipped, 1402 collected, 0 failed, 0 errors |
| Phase 10B targeted | 231 PASS（grammar 94 + API/policy/transaction 132 + migration 5） |
| Phase 10A regression | 63 + 3 PASS；schema lifecycle 13 PASS |
| Frontend lint / build | PASS / PASS |
| Memory UI interaction | PASS（disposable DB + fake provider harness） |
| Real PostgreSQL | NOT RUN（门控跳过） |
| Real Provider Gate | NOT RUN（未授权） |
| Real DB Gate | NOT RUN（未授权；真实库仍为 `9d0e1f2a3b4c`） |

### Phase 10B — SDK 3.24 environment fix（2026-10-05，当时为 `9040c83` 上的未提交工作区，现已包含在 final checkpoint 中）

**历史记录**（按时间顺序保留）。环境：Python 3.12.10、openai 3.24.0、httpx2 2.13.1、httpcore2 2.13.1、fastapi 0.142.2、pydantic 2.13.5、SQLAlchemy 2.1.3、pytest 9.1.1。

| 项 | 结果 |
| --- | --- |
| `python-multipart` 声明 / 安装 | `requirements.txt` 已声明；已安装 0.0.32；`pip check` PASS |
| `app.main` import / Documents API 注册 / pytest collection | PASS / PASS / PASS（disposable `DATABASE_URL`） |
| SDK privacy tests（`test_phase10b_sdk_log_privacy.py`） | 33 PASS（native `httpx2.MockTransport` + 默认 client 的 `127.0.0.1` loopback；真实 httpcore2 DEBUG 日志已触发） |
| Provider / embedding / Phase 5C | 123 PASS |
| Phase 10B targeted | 308 PASS |
| Phase 10A regression | 66 PASS |
| Migration / schema | 23 PASS |
| Full pytest（首轮，仅 `python-multipart` 修复后） | 1479 collected：1431 passed，11 failed，37 skipped，0 errors，exit code 1 |
| 文档依赖修复：`pypdf` / `python-docx` 声明 / 安装 | `requirements.txt` 已声明；已安装 pypdf 6.19.0、python-docx 1.2.0（连带 lxml 6.1.3）；`pip check` PASS |
| 原 11 个失败用例 | 11 PASS |
| Document / parser regression | 135 PASS |
| **Full pytest（最终）** | **1479 collected：1442 passed，0 failed，37 skipped，0 errors，exit code 0** |
| Real Provider / Real DB（当时） | 未调用 / 未迁移 |

首轮的 11 个失败全部来自同一原因：文档解析器使用的 `pypdf` 与 `python-docx`（import 名 `docx`）是**未声明的运行期依赖**（pre-existing dependency manifest defect）。Astra 批准后已在 `requirements.txt` 声明并经 manifest 安装；未修改 parser / document service / 测试代码。37 个 skipped 均为门控（真实 PostgreSQL / 真实 Provider 冒烟）。

已记录技术债（Astra 接受）：Phase 9A `secure_library_logging()` 只压制 legacy `httpx` / `httpcore`，不覆盖 `httpx2` / `httpcore2`。

### Phase 10B — FINAL ACCEPTED（Astra，2026-10-06）

| 项 | 最终结果 |
| --- | --- |
| Phase 10B | **CLOSED / ACCEPTED** |
| Implementation / Regression | PASS / PASS |
| Fresh-environment full backend pytest | **1479 collected：1442 passed，37 skipped，0 failed，0 errors，exit code 0** |
| Runtime | Python 3.12.10；openai 3.24.0；httpx2 2.13.1；httpcore2 2.13.1；python-multipart 0.0.32；pypdf 6.19.0；python-docx 1.2.0；lxml 6.1.3 |
| Real Provider Gate | **PASS**（deepseek / `deepseek-v4-flash`；授权真实业务调用 2，实际 2；SDK transport 重试 0；使用 disposable DB） |
| Real DB Gate | **PASS**（真实库 revision `ae1f2a3b4c5d`；Memory rows 0；历史 receipts 2；AUTOINCREMENT high-water 2；`quick_check` ok） |
| Real PostgreSQL | NOT RUN（Phase 10B 未在真实 PostgreSQL 上执行；见 §17） |

37 个 skipped 均为门控（真实 PostgreSQL / 真实 Provider 冒烟），默认跳过而非伪造通过。

### Phase 10C — FINAL ACCEPTED（2026-10-06）

| 项 | 最终结果 |
| --- | --- |
| Phase 10C | **CLOSED / ACCEPTED** |
| Claude implementation / First-Pass Gate | PASS / PASS |
| Final backend regression（final source） | **1682 collected：1645 passed，37 skipped，0 failed，0 errors，exit code 0** |
| Frontend | build PASS；lint PASS；browser QA PASS（Claude 50/50，headless Chrome，disposable backend + fake provider，loopback） |
| Luna Independent QA | **PASS**（Luna 独立针对性证据：236 个 Phase 10C / SDK 测试 + 28 个历史测试，全部 PASS；由 Luna 报告，非本文件作者复跑） |
| Architecture / Implementation Review | PASS |
| Real Provider Gate | **NOT REQUIRED**（离线 transport 拦截为验收证据；未调用任何真实 Provider） |
| Real DB Gate | **PASS**（`ae1f2a3b4c5d` → `b10c0d1e2f3a`；迁移前外部备份逐字节校验；数据 / FK / 索引 / sequence 保留；只读 readiness 通过） |
| Real PostgreSQL | NOT RUN（仅离线 DDL / portability；未执行 live PostgreSQL gate） |

Runtime：Python 3.12.10；openai 3.24.0；httpx2 2.13.1；httpcore2 2.13.1；SQLAlchemy 2.1.3；Node v22.23.2 / npm 10.9.8（`npm ci`，manifest 与 lockfile 未变）。
37 个 skipped 均为门控（真实 PostgreSQL / 真实 Provider 冒烟）。真实库迁移时 `messages` 为 0 行；已有消息行的 false backfill 由 disposable 迁移测试证明，而非真实库。

---

## 14. Development Workflow（三方）

**Claude Code = PRIMARY IMPLEMENTATION ENGINEER**
唯一允许修改产品代码的 AI。职责：implementation、migrations、tests、bug fixes、first-pass regression、authorized real integration smoke。

**GPT-6 Luna Max = INDEPENDENT QA**
默认 READ + TEST ONLY，不得修改产品代码。职责：adversarial testing、edge cases、regression、failure reproduction、QA report。

**GPT-6 Astra = PRINCIPAL ARCHITECT / REVIEWER**
职责：phase specification、architecture、scope control、risk review、evidence review、final phase acceptance。

每个 phase 均为门控：未经 Astra 明确授权，不开始下一阶段、不 commit / push。

---

## 15. Version Discipline

- 任何 QA / Review 必须声明对应的 **commit hash** 或 **exact uncommitted diff state**。
- Claude 修改代码后，旧 Luna QA verdict 对受影响范围**失效**，必须 retest。
- 不要混用不同版本的测试证据。

---

## 16. Protected Files & Real-Environment Boundary

通常受保护、不得随意修改：

- `.env`
- `.env.example`
- `.gitignore`
- `backend/jarvis.db`
- Microsoft token / auth cache（DPAPI，仓库外）
- 已有 migration 历史（`backend/alembic/versions/`）
- 依赖清单 / lockfile（`requirements*.txt`、`package.json`、`package-lock.json`）

这些文件**不是**永久不可变；只有当具体 phase / task 明确授权时才可修改：

- 真实 DB migration：需要显式的 migration gate 授权。
- Microsoft 认证 / consent：需要显式的 integration-smoke 授权；密码 / MFA / passkey 由用户本人完成。
- 已有 migration 文件：验收后不得修改，除非有专门的恢复流程明确授权。
- 依赖：不得随意新增或变更；必须由 phase 规格授权。
- 真实外部副作用（如真实 Outlook 事件、真实 LLM 调用消耗额度）：必须显式授权。

---

## 17. Current Technical Debt（记录，不修）

| 项 | 分类 |
| --- | --- |
| Calendar Disconnect 历史观察（非阻塞）：disconnect 后 UI 短暂显示通用后端错误，而后端 disconnect 状态正确。Root cause：NOT DETERMINED / NOT REPRODUCED IN CURRENT REVIEW | 未分类 |
| real expired-token refresh not observed | BEFORE PLANNING |
| real response-loss not intentionally exercised | BEFORE MULTI-WRITE PLANNING EXECUTION |
| preview persistence limitations | BEFORE PLANNING（Planning 需要处） |
| minimal datetime parser | BEFORE PLANNING（datetime semantics） |
| DST consistency | BEFORE PLANNING |
| HTTP transport request-count observability | BEFORE MULTI-WRITE PLANNING EXECUTION |
| single-user local security boundary | ONLY IF DEPLOYMENT CHANGES（full authentication / multi-user isolation） |
| real PostgreSQL integration not verified（本 baseline） | ONLY IF DEPLOYMENT CHANGES（real PostgreSQL deployment validation） |
| SQLite `value_json`（JSON → NUMERIC affinity）会丢失历史手动值的 float/int 类型（`45.0` 读回 `45`）；Phase 10B 比较实际读回值，无法恢复已丢失的类型（Astra：非阻塞既有限制） | 未来存储模型考虑（未修复） |
| 依赖可复现性不完整：`requirements*.txt` 多为下界约束，没有完整 lock 策略；Phase 10B 期间恢复的环境因此暴露出 3 个未声明的运行期依赖（已补声明），未来环境仍可能解析到不同版本 | Phase 10B 已接受 / 延后（未修复） |
| Phase 9A `secure_library_logging()` 只压制 legacy `httpx` / `httpcore` logger；openai 3.24 的默认栈 `httpx2` / `httpcore2` 不在其名单内 | Phase 10B 已接受 / 延后（未修复） |
| httpcore2 在 DEBUG 记录完整响应头（可能含 request id、限流元数据、cookie、供应商字段），未做脱敏；原始 DEBUG 日志不得作为可公开 / 可分享证据 | Phase 10B 已接受残留 |
| Memory pending proposal 仅存于单进程内存：重启丢失、不支持多 API 进程 | Phase 10B 已接受限制（V1 单进程） |
| Phase 10B 未在真实 PostgreSQL 上执行（迁移与流程仅离线验证） | ONLY IF DEPLOYMENT CHANGES |
| 工作区中与 Phase 10B / 10C 无关的既有状态：`.gitignore` 的未提交修改、未跟踪的 `apisix-docker/`；未纳入任何 Phase 10B / 10C commit，也未清理 | 无关既有状态（不属于 Phase 10B / 10C） |
| 既有前端缺陷（早于 Phase 10C，未修改）：重新加载包含「无 task 的 REJECTED 任务建议」的对话时，ChatView 可能白屏 | 未修复（非阻塞，不属于 Phase 10C） |
| 普通 Chat 的 request session 在 Provider 生成期间保持打开（既有行为）；Phase 10C 只保证 Memory selector 的独立短会话在调用 Provider 前关闭，未重构 Chat 事务生命周期 | 未来加固（Phase 10C 已接受） |
| Provider 原生 tokenizer 预算不在范围内：Phase 10C 使用确定性的 byte-token accounting units（UTF-8 字节 + 固定信封预留），不代表任何 Provider 的原生 token 数；整段对话的 token 预算同样未做 | Phase 10C 已接受边界 |
| Phase 10C 未执行 live PostgreSQL gate（迁移仅离线 DDL / portability 验证） | ONLY IF DEPLOYMENT CHANGES |
| 被标记的 Chat 回复不进入后续自动上下文：用户若要就该回复追问，需重新勾选当前偏好或直接引用相关文字（设计取舍，非缺陷） | Phase 10C 已接受行为 |
| Phase 10C 浏览器 QA 经既有的日历状态检查在 `%LOCALAPPDATA%\JARVIS\auth` 留下一个空目录；未创建任何凭据 / token 文件，未调用 Microsoft | 非阻塞环境副作用（未清理） |

按阶段汇总：

- **BEFORE PLANNING**：expired-token confidence；Preview persistence where Planning needs it；datetime semantics；DST consistency
- **BEFORE MULTI-WRITE PLANNING EXECUTION**：response-loss / HTTP write observability
- **ONLY IF DEPLOYMENT CHANGES**：full authentication / multi-user isolation；real PostgreSQL deployment validation

---

## 18. Phase 10B Architecture — FROZEN / CLOSED / ACCEPTED

**Phase 10B：Explicit Memory Proposal & Confirmation**（Astra 已冻结规格并于 2026-10-06 验收关闭。实现细节见 `docs/phase10b-memory-proposals.md`）

```
Natural Language
→ candidate
→ typed validation
→ program policy
→ Preview
→ Explicit Confirm
→ Memory write
```

- 不做 silent auto-memory。
- 建议采用 **independent Memory Proposal / Confirm domain**，而不是直接复用完整 `action.confirm` Execution payload——避免在 Execution 回执中长期复制 Memory value。

---

## 19. Future Roadmap（可在架构评审后变更，均未授权）

| Phase | 方向 |
| --- | --- |
| 10B | Explicit Memory Proposal（**CLOSED / ACCEPTED**） |
| 10C | Explicit Per-Request Personal Memory Use in Ordinary Chat（**CLOSED / ACCEPTED**） |
| 11A | Read-only Planning V1 |
| 11B | Confirmed Planning Execution |

Roadmap can change after architecture review.

---

## 20. Key References

- Memory 规格：`docs/phase10a-memory.md`
- Phase 10C Frozen Spec 与验收记录：`docs/specs/phase10c-frozen-spec.md`；实现入口：`backend/app/services/memory_context_service.py`、`backend/app/services/chat_service.py`、`backend/app/llm/messages.py`
- Schema lifecycle：`backend/app/services/schema_lifecycle.py`
- Execution registry：`backend/app/services/execution_registry.py`
- Action Proposal / Confirm：`backend/app/services/action_proposal_service.py`、`backend/app/services/action_confirm_service.py`
- Calendar integration：`backend/app/integrations/calendar/`
- Migrations：`backend/alembic/versions/`
- README（历史阶段说明较多；部分章节为阶段性记录）：`README.md`
