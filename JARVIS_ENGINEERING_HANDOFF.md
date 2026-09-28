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
| Phase 10B | NOT STARTED |

- Current open development phase: **NONE**
- Next candidate: **Phase 10B — Explicit Memory Proposal & Confirmation**
  - **NOT AUTHORIZED FOR IMPLEMENTATION**

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
| Personal Memory | Phase 10A 手动 CRUD（见 §5） | `/api/memories`；前端「记忆」tab |

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

**当前不存在**：Natural Language → Memory write。

---

## 5. Memory Contract（Phase 10A 正式 contract）

详细规格：[`docs/phase10a-memory.md`](docs/phase10a-memory.md)。

- Categories：`preference`、`stable_fact`、`constraint`（仅此三类）
- Memory 性质：user-visible、editable、deletable、source-aware、version protected
- `source_type`：当前仅 `manual`（服务端控制，客户端不可提供）
- value：bounded structured JSON（标量 / ≤64 标量列表 / ≤32 字段扁平对象；无 null、无嵌套；紧凑 JSON ≤ 4096 UTF-8 bytes）
- key：1–80 ASCII，小写字母开头，`[a-z0-9_]`；单用户内全局唯一（重复 409 `MEMORY_KEY_CONFLICT`）
- version：optimistic concurrency（`expected_version` 数据库谓词 CAS；陈旧 409 `MEMORY_VERSION_CONFLICT`；无自动重试、无静默覆盖）
  - 注：`expected_version` 冲突保护当前仅适用于 **UPDATE**；当前 **DELETE** 按 ID 删除，不要求 `expected_version`。
- delete：application-level hard delete（无历史、无 tombstone、无 undo；**不是**存储/备份层面的安全擦除）

明确**不存在**：

- NO automatic memory
- NO LLM extraction
- NO context injection
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
- Current unique Alembic head：**`9d0e1f2a3b4c`**（`personal_memories`，down_revision `8c9d0e1f2a3b4`；迁移链线性，共 13 个 revision）
- Current real SQLite DB：`backend/jarvis.db`
  - Alembic revision：`9d0e1f2a3b4c`
  - `personal_memories`：present
  - current Memory rows：0
  - 以上状态于 2026-09-28 以只读方式（`mode=ro`）复核。这是最近验收状态，不是永久规格；DB 二进制哈希不作为规格记录。
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

按阶段汇总：

- **BEFORE PLANNING**：expired-token confidence；Preview persistence where Planning needs it；datetime semantics；DST consistency
- **BEFORE MULTI-WRITE PLANNING EXECUTION**：response-loss / HTTP write observability
- **ONLY IF DEPLOYMENT CHANGES**：full authentication / multi-user isolation；real PostgreSQL deployment validation

---

## 18. Next Planned Architecture — PLANNED / NOT IMPLEMENTED

**Phase 10B：Explicit Memory Proposal & Confirmation**（Astra 当前推荐方向，未授权）

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
| 10B | Explicit Memory Proposal |
| 10C | Bounded Memory Selection / Transparent Context Use |
| 11A | Read-only Planning V1 |
| 11B | Confirmed Planning Execution |

Roadmap can change after architecture review.

---

## 20. Key References

- Memory 规格：`docs/phase10a-memory.md`
- Schema lifecycle：`backend/app/services/schema_lifecycle.py`
- Execution registry：`backend/app/services/execution_registry.py`
- Action Proposal / Confirm：`backend/app/services/action_proposal_service.py`、`backend/app/services/action_confirm_service.py`
- Calendar integration：`backend/app/integrations/calendar/`
- Migrations：`backend/alembic/versions/`
- README（历史阶段说明较多；部分章节为阶段性记录）：`README.md`
