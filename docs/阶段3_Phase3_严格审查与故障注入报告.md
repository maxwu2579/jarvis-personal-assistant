# Phase 3（Reminder 提醒调度）严格代码审查与故障注入报告

审查范围：仅已实现的 Phase 3（Reminder/Notification）。未开发下一阶段，未新增邮件、Calendar、PDF、RAG 功能。
方法：不信任既有报告——重新读取全部实际代码、迁移与测试，并用故障注入与反例验证。基准修复前为 140 passed, 2 failed（既有报告声称 122 passed 不准确）。

---

## 1. 发现的真实问题（按严重程度排列）

### 高
| # | 问题 | 证据 | 后果 |
|---|------|------|------|
| H1 | **测试基础设施：测试引擎未启用 SQLite 外键**（`tests/conftest.py` 的 `client` 与 `worker_env` 引擎没有挂 `_set_sqlite_pragma` 监听器） | `conftest.py` 原代码只对生产 `engine` 挂监听器；`test_notification_fk_violation_rejected`、`test_session_usable_after_rollback` 均因此无法触发 `IntegrityError` 而失败 | 外键约束（CASCADE/拒绝插入）在测试中完全不生效——测试与生产行为分叉，外键相关回归无法被发现 |
| H2 | **测试自身缺陷：`_assert_real_indexes` 查询只 SELECT 1 列却访问 `notif[0][1]`** | `SELECT sql FROM sqlite_master ...` → 每行是 1 元组 → `notif[0][1]` 抛 `IndexError: tuple index out of range`（此前被误判为「索引在测试库缺失」） | 索引验证测试无效；误导排查方向 |
| H3 | **测试顺序污染：Alembic `env.py` 的 `fileConfig(disable_existing_loggers=True)` 禁用全部既有 logger** | `tests/test_migrations.py` 先跑 `command.upgrade` → `reminder_worker` logger 被禁用 → 后续 `test_worker_tick_error_log_is_sanitized` 的 caplog 捕获不到任何记录 | 日志脱敏测试依赖测试执行顺序，全量跑必挂 |

### 中
| # | 问题 | 证据 | 后果 |
|---|------|------|------|
| M1 | **`test_db_failure_rollback` 断言 `stats.claimed == 1` 与实际语义矛盾** | 异常在 `_claim_and_deliver` 返回前抛出，outcome 尚未记录 → `claimed` 计 0；领取成功由数据库状态（CLAIMED + attempt=1）证明 | 断言与实现语义不符；误导读者 |
| M2 | **`--once` 退出码测试缺陷**：成功路径指向空库（无表），失败路径断言了错误的错误文案（`非法时间格式`，实际是 fromisoformat 的 ValueError 消息） | subprocess 实测 | 退出码区分成功/失败的验证无效 |
| M3 | **raw `text()` SQL 绑定 aware datetime** | aware 值经 sqlite3 适配器存成 `"2020-01-01 00:00:00+00:00"` 字符串，恢复轮询时 `db.refresh` 解析抛 `TypeError` | 测试本身不稳定，且掩盖「raw SQL 必须绑 naive」的约定 |
| M4 | **README 过时**：标题仍写 "Phase 0 + Phase 1"；「当前未实现」清单仍列 Reminder；「下一阶段计划」第一项仍是 Reminder | `README.md` L1/L52/L274/L284 | 文档与实现矛盾，破坏项目可信度 |
| M5 | **`tests/test_migrations.py` EXPECTED_TABLES 缺 reminders/notifications** | 005 是 head，迁移建库必须含 Phase 3 两表 | 迁移完整性断言不覆盖 Phase 3 |

### 低
| # | 问题 | 证据 | 后果 |
|---|------|------|------|
| L1 | `test_partial_unique_index_exists_in_migrated_db` 与 real-app-db 变体依赖测试执行顺序（迁移测试先跑会禁 logger，反之不会） | 见 H3 | 与 H3 同源，修复 H3 后消失 |

## 2. 修复内容

- **H1**：`tests/conftest.py` — `client` 与 `worker_env` 引擎都挂 `event.listen(engine, "connect", _set_sqlite_pragma)`；`test_session_usable_after_rollback` 自建引擎同样挂载。
- **H2**：`tests/test_phase3_review.py` — `_assert_real_indexes` 查询改为 `SELECT name, sql`（与 `active` 查询同构）。
- **H3**：`tests/test_phase3_hardening.py::test_worker_tick_error_log_is_sanitized` — 显式 `monkeypatch.setattr(logger, "disabled", False)` 并注释根因（生产 Worker 进程从不经过 fileConfig，日志正常；此为纯测试顺序污染）。
- **M1**：`tests/test_reminders.py::test_db_failure_rollback` — 断言改为 `stats.claimed == 0`，注释说明「领取成功由数据库状态证明，计数器在异常逃出前未记录」。
- **M2**：`test_once_success_exit_code_0` 改用 client 夹具的库（有表 + 有到期 Reminder）；`test_once_bad_now_exit_code_1` 只断言 `returncode == 1` + `startup_failed` 在 stderr。
- **M3**：`test_delivery_commit_sqlalchemy_error_is_retryable` — raw SQL 绑定改 naive datetime（`datetime(2020, 1, 1, 0, 0)`）。
- **M4**：`README.md` — 标题补 Phase 2/3；未实现清单移除 Reminder 并改为「外部通知渠道未实现」；下一阶段计划第一项改为「Phase 4：Document Analysis 与 RAG（候选，尚未开始）」。
- **M5**：`tests/test_migrations.py` — `EXPECTED_TABLES` 加入 `reminders`、`notifications`；downgrade 断言补充 Phase 3 表移除；注释同步。
- **文档**：`scripts/docs/content_phase3.py` 新增「深入话题：可靠性细节」（6 个知识点）、「故障注入」教学小节、「安全边界：单用户限制」章节，并重新生成 DOCX（详见第 12 项）。

## 3. 未修改但确认正确的关键设计

逐项核对实际代码后确认（全部有测试固定）：

1. **事务边界**：`task_service.py` 的 confirm/postpone/complete 均 `commit=False` 由 API 层统一 commit——Task 状态与 Reminder 副作用同事务；`flush` 失败整体回滚，Task 不残留 CONFIRMED。
2. **Worker 原子领取**：条件 UPDATE（`status=PENDING AND remind_at<=now`）+ rowcount 检查 + 单独提交，SQLite 单写者串行化保证「谁拿到就是谁的」。
3. **lease 边界**：PENDING 用 `<=`、CLAIMED 用 `<`（恰好等于不可领取，避免提交中途被抢造成双投递窗口）。
4. **attempt_count 只加一次**：在领取事务内 +1；竞争失败（rowcount=0）不增加；`last_error_code` 只在 FAILED 时写入。
5. **幂等投递**：`Notification.reminder_id` 唯一约束；冲突时查既有记录 → 幂等 DELIVERED；无记录 → FAILED（`DELIVERY_UNIQUE_CONFLICT`）终态，不无限重试。
6. **per-item 异常隔离**：循环内 `except Exception → rollback → continue`，单条失败不阻塞批次；SQLAlchemyError 返回 claimed 保持 CLAIMED 等 lease 恢复。
7. **部分唯一索引**：`uq_reminders_active_task`（WHERE status IN PENDING/CLAIMED）真实存在，sqlite_master 验证通过；重复 confirm 撞约束 → IntegrityError → 回滚 → 幂等成功。
8. **create_all 双路径警示**：`main.py` lifespan 检测「有表但无 alembic_version」时打响亮 warning，README 指引 `alembic stamp head`；不静默跳过迁移。
9. **日志脱敏**：`sanitize_error_message` 去数据库 URL / API Key + 截断 500 字符；`_safe_tick` 只记录异常类型 + 脱敏消息。
10. **Worker 不执行 LLM 输出**：无任何新执行面；Phase 2 白名单是唯一 LLM 动作通道。
11. **前端竞态**：TaskSection/NotificationCenter 均有 mountedRef 守卫；App 轮询为 setTimeout 链 + cancelled 标志 + 清理，无重叠；NotificationCenter 一次取满 200 条 + 服务端分页上限。
12. **已读幂等**：`read_at is None` 才写入，重复标记不改结果；unread count 由 COUNT 查询保证与列表一致。
13. **退出码契约**：`--once` 成功 0 / 启动失败 1（`SystemExit(1)` + `startup_failed` 日志），实测验证。

## 4. 新增测试

`tests/test_phase3_hardening.py`（新文件，21 个测试）：
- 幂等：唯一冲突已存在通知 → DELIVERED（attempt=1，不无限重试）；唯一冲突无记录 → FAILED 终态
- 投递提交 SQLAlchemyError（真实 OperationalError）→ claimed 可重试、lease 恢复后恰好一次
- 外键：Notification 引用不存在行 → IntegrityError；删除 Task → Reminder/Notification CASCADE 无孤儿
- 批次隔离：第一条领取提交失败 → PENDING attempt 不变，第二条仍 DELIVERED
- 确定性：同 remind_at 按 id 升序投递
- 状态机边界：CONFIRMED 无 Reminder 历史数据 postpone 不新造提醒；mark_read 用注入时钟（精确到 FIXED_NOW）
- Session rollback 后可继续查询/提交/关闭
- 日志脱敏：单测 + `_safe_tick` 集成（caplog）
- `--once` 退出码：成功 0 / 非法时间 1 / 数据库不可用 1（且路径被脱敏）
- 迁移：003 带数据 → head → downgrade 003（列/索引移除、数据保留）→ re-upgrade；REJECTED 行降级响亮失败（NOT NULL）；create_all 库 upgrade 响亮失败 + stamp head 后成功
- unread count 与列表一致（批量已读）

`tests/test_phase3_review.py` 与 `test_reminders.py`：既有故障注入测试修正为正确语义（flush 失败回滚、投递提交失败、claim 竞争等）。

## 5. pytest 精确结果

```
163 passed, 1 warning in 50.76s
```

- 唯一的 warning：`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated` —— 来自 FastAPI/Starlette 上游（httpx 适配弃用），非本项目代码；`pytest.ini` 已过滤的 sqlite3 datetime 适配器 DeprecationWarning 不再出现。
- 修复前基线：140 passed, 2 failed。

## 6. lint / build 结果

- `npm run lint`：通过（0 错误 0 警告）
- `npm run build`：通过（vite build，163.03 kB JS / 8.56 kB CSS，783ms）

## 7. 迁移与索引验证

- **Alembic 闭环**（测试内实证）：003（66405497a397）带 3 种状态 Task + 对话消息 + SUCCEEDED Proposal → upgrade head（reminders/notifications 出现且为空）→ downgrade 003（新表移除、error_code/error_detail 列移除、ix_task_proposals_status 移除、数据原样保留）→ re-upgrade head 全恢复。
- **REJECTED 行降级**：`IntegrityError: NOT NULL constraint failed` 响亮失败，无静默数据损坏。
- **create_all 库**：`alembic upgrade` 抛 `OperationalError: already exists`（不静默跳过）；`alembic stamp head` 后 upgrade 幂等成功。
- **sqlite_master 实测**：
  - `uq_reminders_active_task` → `CREATE UNIQUE INDEX ... ON reminders (task_id) WHERE status IN ('PENDING', 'CLAIMED')` ✓（create_all 库与迁移库均存在）
  - `ix_notifications_reminder_id` → `CREATE UNIQUE INDEX ... ON notifications (reminder_id)` ✓
- 迁移链：001 c26f1e0161a6 → 002 4756c9756074 → 003 66405497a397 → 004 a1b2c3d4e5f6 → 005 fa94512e0868（head）。

## 8. 故障注入结果

10 类注入点全部实测通过（见第 4 项清单）。关键行为：
- Reminder 创建 flush 失败 → HTTP 500，Task 不残留 CONFIRMED（回滚证明）
- 领取提交失败 → PENDING + attempt 不变（竞争失败不增 attempt）
- 投递提交失败（SQLAlchemyError）→ 保持 CLAIMED，lease 过期恢复后恰好一次通知
- 唯一冲突 = 幂等成功（已有通知）或 FAILED 终态（无记录）
- 单批第一条失败 → 第二条仍投递
- 异常不冒泡（per-item 隔离），Worker 循环不崩溃

## 9. HTTP 与 Worker 回归

真实进程闭环（临时库 `loop.db`，端口 8765，验证后删除）：
1. `curl POST /api/tasks`（due_at 已到期）→ 201
2. `curl POST /api/tasks/1/confirm` → CONFIRMED；`GET /api/reminders` → PENDING
3. `python -m app.workers.reminder_worker --once` → `stats=WorkerStats(scanned=1, claimed=1, delivered=1, failed=0)`，exit 0
4. `GET /api/notifications` → 1 条「任务提醒：loop task」
5. **重复 Worker 竞争**：新任务 confirm 后同时启动两个 `--once` → 一个 `claimed=1 delivered=1`，另一个 `scanned=0`；最终 2 条 Reminder 均 DELIVERED（attempt=1），恰好 2 条通知，无重复
6. `POST /api/notifications/1/read` → `read_at` 写入；`unread-count` → 1
7. 关闭 uvicorn，删除临时库；开发库 `jarvis.db` 复查：`alembic_version=fa94512e0868`，全部 6 表 0 行——与基线一致，**未污染**

无 Key 模式回归：应用在 `llm_api_key` 为空时正常启动，Task/Reminder API 正常（本次闭环即运行于无 Key 环境）；`LLM_NOT_CONFIGURED` 由 `tests/test_chat.py` 与 `tests/test_task_proposals.py` 固定。

## 10. SQLite 仍然存在的限制（诚实声明）

1. **单写者模型**：SQLite 同一时刻一个写事务——竞争处理靠条件 UPDATE + 单写者串行化，未证明（也不承诺）分布式多写者语义；并发测试是「顺序模拟」，不是真实多线程。
2. **不承诺 exactly-once**：实现 at-least-once + 去重（唯一约束），重复投递被去重为一条通知。
3. **外键必须显式开启**：每个连接需 `PRAGMA foreign_keys=ON`（已通过事件监听器统一处理，但任何新建引擎都必须同样挂载）。
4. **raw SQL 时间绑定约定**：text() SQL 必须绑 naive UTC，aware 值会因 sqlite3 适配器存成字符串导致解析失败。
5. **PostgreSQL 迁移是「方向」不是「已完成」**：Alembic 链就绪，但当前 SQL 以 SQLite 为准，逐条迁移的 PG 分支需另行验证；放开单写者假设后竞争测试需重写。
6. **通知为应用内**：无邮件/短信/微信等外部渠道。

## 11. 浏览器 UI 是否真实验证

**未做真实浏览器点击**。本环境无法启动真实浏览器进行点击级验证，且用户任务明确不新增功能。已做到的替代验证：
- `npm run build` 通过（TypeScript 类型检查 + 打包）
- 前端竞态/卸载守卫逻辑逐行审查（mountedRef、refreshSeq、setTimeout 链无重叠）
- API 契约（分页参数、统一 422、排序）由后端测试固定，前端调用方已同步

建议：后续阶段（或 CI 环境）引入 Playwright 冒烟（打开页面 → 建任务 → confirm → 等通知中心出现通知 → 标记已读 → 刷新恢复），作为 Phase 4 的验证项。

## 12. Phase 3 DOCX 是否重新生成及新页数

**已重新生成**：
- 更新 `scripts/docs/content_phase3.py`：新增「深入话题：可靠性细节」6 个知识点（SQLite 外键 PRAGMA、部分唯一索引、lease 边界 ≤/<、CLAIMED 期间 postpone/complete 策略、列表分页与确定性排序、PostgreSQL 迁移方向）；「测试教学」新增故障注入小节（模式 + 代码示例 + 误解澄清）；新增「安全边界：单用户限制」章节。
- 重新生成 `docs/learning/阶段3_Phase3_Reminder提醒调度.docx`（81.4 KB）。
- `verify_docs.py`：全部检查通过（中英配对、Heading 层级、表格非空、代码块底纹、无 box-drawing、无 U+FFFD）。
- Word COM 渲染（word_render_check.ps1）：**58 页**（原 54 页），TOC 更新 + Repaginate 成功，导出 PDF。
- 渲染 PDF 复核：58 页无空白页、无替换字符（乱码）。

## 13. 是否建议进入 Phase 4（Document Analysis 与 RAG）

**建议：可以进入，但需先完成以下前置（门槛条件）**：

建议进入的理由：
- Phase 3 代码质量经本轮修复后达到基线要求：163 测试全绿（含 10 类故障注入）、lint/build 干净、迁移闭环验证、开发库未污染；
- 事务边界/Worker/幂等设计经反例验证正确，可作为后续阶段的可靠性范式。

进入 Phase 4 前的门槛（建议作为 Phase 4 计划第一项）：
1. **浏览器 UI 冒烟**：引入 Playwright 补上第 11 项缺口（本阶段唯一的验证空白）；
2. **PostgreSQL 分支验证**：若 Phase 4 涉及文档存储/检索，先把迁移链的 PG 分支逐条验证（否则多库漂移风险累积）；
3. **README 与学习文档已同步**（本轮已完成），Phase 4 计划需更新下一阶段预告。

不建议立即进入的理由（如优先做）：
- 若下一目标只是继续打磨可靠性（如外部通知渠道、任务队列抽象），Phase 3 范式可平滑扩展；Document Analysis 涉及文件存储、解析器、embedding 依赖与更宽的注入面，建议在 UI 冒烟与 PG 分支就绪后再动工。

**结论：建议进入 Phase 4，但以「Playwright UI 冒烟 + PostgreSQL 迁移分支验证」为前置门槛，并在 Phase 4 计划中显式声明 RAG 的隐私/注入边界（单用户限制、仅本地解析）。**

---

## 附：验证环境与清理记录

- 后台进程：uvicorn（端口 8765）已停止（TaskStop 确认）
- 临时数据库：`.pytest_tmp/live_loop/loop.db` 已删除；`.pytest_tmp` 测试根已清空
- 临时工具：`.pytest_tmp/pdfcheck`（pypdf，仅验证用）已删除
- 开发库：`backend/jarvis.db` — `alembic_version=fa94512e0868`，6 表 0 行，与基线完全一致
