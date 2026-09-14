"""Phase 3 学习文档内容：Reminder 提醒调度与应用内通知。

遵循项目文档规范：中英段落逐段配对、知识点固定五段式、代码讲解五段式、
代码示例全部来自 JARVIS 仓库真实文件；架构图与状态转换图一律用表格呈现。
"""

META = {
    "title_cn": "阶段 3：Reminder 提醒调度",
    "title_en": "Phase 3: Reminder Scheduling",
    "subtitle_cn": "JARVIS 项目双语学习文档 · 数据库驱动的提醒与通知中心",
    "subtitle_en": "Bilingual learning material for the JARVIS project",
    "header": "JARVIS 双语学习 · 阶段 3 / Phase 3",
}


def _knowledge_point(
    doc,
    title_cn,
    title_en,
    explain_cn,
    explain_en,
    keywords,
    instance_cn,
    instance_en,
    practice,
):
    doc.add_heading(title_cn, title_en, level=2)
    doc.add_label("1. 中文解释")
    doc.add_cn_only(explain_cn)
    doc.add_label("2. English Explanation")
    doc.add_en_only(explain_en)
    doc.add_label("3. 关键词 / Key words")
    for cn, en in keywords:
        doc.add_bullet_pair(f"{cn} — {en}")
    doc.add_label("4. 项目实例 / Project example")
    doc.add_pair(instance_cn, instance_en)
    doc.add_label("5. 英语表达练习 / Speaking practice sentence")
    doc.add_pair(practice[0], practice[1])


def build(doc):
    # ---------------- 文档说明与学习目标 ----------------
    doc.add_heading("文档说明与学习目标", "About this document and learning goals", level=1)
    doc.add_pair(
        "Phase 3 让 JARVIS 拥有「时间感」：任务确认后自动安排到期提醒，独立 Worker 轮询数据库投递应用内通知，"
        "前端通知中心展示并支持标记已读。本阶段的核心工程主题是后台任务（background jobs）与可靠性："
        "原子领取、租约、幂等投递、崩溃恢复——这些都是面试与真实后端的高频话题。",
        "Phase 3 gives JARVIS a sense of time: confirming a task schedules a reminder, an independent worker "
        "polls the database and delivers in-app notifications, and the frontend notification center shows "
        "them with read marking. The core engineering themes are background jobs and reliability: atomic "
        "claiming, leases, idempotent delivery, and crash recovery — all high-frequency interview topics.",
    )
    doc.add_pair(
        "学习目标：① 理解 Worker 与 Scheduler 的区别；② 掌握数据库轮询、原子领取与 lease；③ 理解幂等性与"
        "exactly-once 为什么困难；④ 学会用 Fake Clock 测试时间敏感逻辑；⑤ 用中英文解释这些可靠性设计。",
        "Learning goals: ① understand the difference between a worker and a scheduler; ② master database "
        "polling, atomic claiming, and leases; ③ understand idempotency and why exactly-once is hard; ④ "
        "learn to test time-sensitive logic with a Fake Clock; ⑤ explain these reliability designs in "
        "Chinese and English.",
    )

    # ---------------- 零基础概念 ----------------
    doc.add_heading("零基础概念：后台任务是什么", "Beginner concept: what a background job is", level=1)
    doc.add_pair(
        "想象一个餐厅：前台（API）接待顾客点单，后厨（Worker）按顺序做菜。如果前台一边接待顾客一边做菜，"
        "客人稍多就会手忙脚乱。JARVIS 也一样：确认任务时只「下订单」（创建 PENDING Reminder），"
        "真正「做菜」（到期投递通知）由独立的 Worker 进程在后台完成。这样 API 永远快速响应，"
        "耗时工作不会阻塞用户请求。",
        "Imagine a restaurant: the front desk (the API) takes orders while the kitchen (the Worker) cooks in "
        "the background. If the front desk cooked while serving, things would fall apart under load. JARVIS "
        "works the same way: confirming a task only places the order (creates a PENDING reminder), while the "
        "actual cooking (delivering the notification at due time) happens in a separate worker process. The "
        "API stays fast, and slow work never blocks user requests.",
    )

    # ---------------- 架构图 ----------------
    doc.add_heading("中英双语架构图", "Bilingual architecture diagram", level=1)
    doc.add_architecture_table(
        "Phase 3 整体架构：Task 确认到 Notification 的完整闭环",
        "Phase 3 architecture: the full loop from task confirmation to notification",
        [
            (
                "① Task 确认（API 进程）",
                "① Task confirmation (API process)",
                "confirm 接口把 DRAFT 变为 CONFIRMED；若有 due_at，同一事务创建 PENDING Reminder。",
                "The confirm endpoint turns DRAFT into CONFIRMED; if due_at exists, a PENDING reminder is created in the same transaction.",
            ),
            (
                "② Reminder 表",
                "② The reminders table",
                "数据库是唯一事实来源：状态、到期时间、租约、尝试次数都存在这里。",
                "The database is the single source of truth: status, due time, lease, and attempt counts live here.",
            ),
            (
                "③ Reminder Worker（独立进程）",
                "③ Reminder Worker (separate process)",
                "轮询 PENDING 且到期的记录 → 原子领取（CLAIMED + lease）→ 创建 Notification → DELIVERED。",
                "Polls due PENDING records, claims them atomically (CLAIMED + lease), creates a notification, and marks DELIVERED.",
            ),
            (
                "④ Notification 表",
                "④ The notifications table",
                "一条 Reminder 至多一条通知（唯一约束 = 幂等投递的最终保障）。",
                "At most one notification per reminder (unique constraint = the final guarantee of idempotent delivery).",
            ),
            (
                "⑤ 前端通知中心",
                "⑤ Frontend notification center",
                "未读计数、通知列表、标记已读（幂等），刷新后从 API 恢复。",
                "Unread count, notification list, and idempotent mark-read; state is restored from the API after a refresh.",
            ),
        ],
    )
    doc.add_pair(
        "关键点：Worker 与 API 是两个独立进程，共享同一个数据库——没有内存里的计时器，重启任何一方都不丢状态。",
        "Key point: the worker and the API are separate processes sharing one database — there is no "
        "in-memory timer, and restarting either side loses nothing.",
    )

    # ---------------- Reminder 状态转换图 ----------------
    doc.add_heading("Reminder 状态转换图", "The Reminder state machine", level=1)
    doc.add_table(
        ("转换 / Transition", "说明 / Meaning"),
        [
            [("PENDING → CLAIMED", "Worker 原子领取：设置 claimed_at、lease_expires_at，attempt_count 加一"),
             ("PENDING → CLAIMED", "The worker claims it atomically: sets claimed_at and lease_expires_at, increments attempt_count.")],
            [("CLAIMED → DELIVERED", "投递成功：创建 Notification 并记录 delivered_at"),
             ("CLAIMED → DELIVERED", "Delivery succeeds: a notification is created and delivered_at is recorded.")],
            [("PENDING → CANCELLED", "任务完成时取消未投递的提醒"),
             ("PENDING → CANCELLED", "Completing the task cancels the not-yet-delivered reminder.")],
            [("CLAIMED → CLAIMED（重试）", "投递遇到可重试错误：保持 CLAIMED，等 lease 过期后重新领取"),
             ("CLAIMED → CLAIMED (retry)", "A retryable delivery error keeps it CLAIMED; it is re-claimed after the lease expires.")],
            [("CLAIMED → FAILED", "尝试次数达到上限（MAX_ATTEMPTS），或遇到永久数据错误"),
             ("CLAIMED → FAILED", "Attempts reach MAX_ATTEMPTS, or a permanent data error occurs.")],
            [("DELIVERED / CANCELLED / FAILED（终态）", "不再被 Worker 扫描，历史保留"),
             ("DELIVERED / CANCELLED / FAILED (terminal)", "No longer scanned by the worker; history is kept.")],
        ],
        widths=[6.0, 10.5],
    )
    doc.add_pair(
        "状态机由数据库字段驱动：Worker 的每次扫描、每次领取都是一次带条件的数据库操作，而不是代码里的内存判断。",
        "The state machine is driven by database fields: every scan and every claim is a conditional database "
        "operation, not an in-memory check.",
    )

    # ---------------- 关键文件 ----------------
    doc.add_heading("关键文件", "Key files", level=1)
    doc.add_table(
        ("文件 / File", "作用 / Purpose"),
        [
            [("backend/app/models/reminder.py", "Reminder 与 Notification 模型、状态枚举、部分唯一索引"),
             ("backend/app/models/reminder.py", "Reminder and Notification models, status enums, and the partial unique index.")],
            [("backend/app/services/reminder_service.py", "领域逻辑：确认/延期/完成联动 + process_due_reminders_once"),
             ("backend/app/services/reminder_service.py", "Domain logic: confirm/postpone/complete hooks plus process_due_reminders_once.")],
            [("backend/app/workers/reminder_worker.py", "独立 Worker 入口：无限轮询、graceful shutdown、--once/--now 测试模式"),
             ("backend/app/workers/reminder_worker.py", "The standalone worker entry: polling loop, graceful shutdown, --once/--now test modes.")],
            [("backend/app/services/task_service.py", "Task 状态机与 Reminder 副作用的事务集成"),
             ("backend/app/services/task_service.py", "The task state machine with transaction-integrated reminder side effects.")],
            [("backend/app/api/reminders.py", "只读 API 与标记已读；无任何创建 Reminder 的公共入口"),
             ("backend/app/api/reminders.py", "Read-only APIs and mark-read; no public endpoint creates reminders.")],
            [("backend/tests/test_reminders.py", "25 个 Fake Clock 测试：投递、竞争、lease、联动、API"),
             ("backend/tests/test_reminders.py", "25 Fake Clock tests: delivery, competition, leases, hooks, and APIs.")],
        ],
        widths=[6.5, 10.0],
    )

    # ---------------- 核心知识点 ----------------
    doc.add_heading("核心知识点", "Core knowledge points", level=1)

    _knowledge_point(
        doc,
        "后台任务：把耗时工作移出请求路径",
        "Background jobs: moving slow work out of the request path",
        "后台任务（background job）是在请求之外执行的工作单元。为什么需要它？因为 API 请求应该快速返回："
        "如果确认任务时要等待提醒投递，用户会卡在请求上。JARVIS 的做法是确认时只写入「提醒待办」记录，"
        "投递由后台 Worker 独立完成。",
        "A background job is a unit of work executed outside the request. Why do we need it? Because API "
        "requests should return fast: if confirming a task waited for reminder delivery, users would hang "
        "on the request. JARVIS writes a reminder record at confirmation and lets a background worker "
        "handle delivery independently.",
        [
            ("后台任务 — Background job", "Work executed outside the request path"),
            ("请求路径 — Request path", "The code a request runs through"),
            ("解耦 — Decoupling", "Separating fast work from slow work"),
        ],
        "app/services/task_service.py 的 confirm_task 只创建 PENDING Reminder；app/workers/reminder_worker.py 负责投递。",
        "confirm_task in app/services/task_service.py only creates a PENDING reminder; "
        "app/workers/reminder_worker.py handles delivery.",
        (
            "“Slow work runs in background jobs, so the API never blocks on it.”",
            "「耗时工作放在后台任务里，API 永远不会被它阻塞。」",
        ),
    )

    _knowledge_point(
        doc,
        "Worker 与 Scheduler 的区别",
        "Worker vs Scheduler",
        "Scheduler（调度器）负责「什么时候运行」——像闹钟，到点提醒你去跑步。Worker（工人）负责「具体做什么」"
        "——像跑步的人。有的系统用调度器触发任务（如每天 9 点跑报表），有的用 Worker 轮询就绪的工作。"
        "JARVIS 的 Reminder Worker 是轮询型 Worker：没有闹钟，它每隔几秒看一眼数据库里有没有到期的工作。",
        "A scheduler decides when things run — like an alarm clock that tells you when to run. A worker "
        "decides what to do — like the runner. Some systems use schedulers to trigger jobs (for example, a "
        "report every day at 9am); others use workers that poll for ready work. JARVIS's reminder worker is "
        "a polling worker: no alarm, it checks the database every few seconds for due work.",
        [
            ("调度器 — Scheduler", "Decides when to run"),
            ("工人 — Worker", "Executes the work"),
            ("轮询 — Polling", "Repeatedly checking for ready work"),
        ],
        "reminder_worker 的轮询循环用 REMINDER_POLL_INTERVAL_SECONDS 控制频率；单次逻辑在 process_due_reminders_once。",
        "The reminder_worker polling loop runs every REMINDER_POLL_INTERVAL_SECONDS; the single-pass logic "
        "lives in process_due_reminders_once.",
        (
            "“We use a polling worker rather than a scheduler: the worker repeatedly checks the database for due reminders.”",
            "「我们用轮询型 Worker 而不是调度器：Worker 反复检查数据库里到期的提醒。」",
        ),
    )

    _knowledge_point(
        doc,
        "数据库轮询：以数据库为唯一事实来源",
        "Database polling: the database is the single source of truth",
        "轮询（polling）就是反复查询：「有没有到期的工作？」。数据库是唯一事实来源意味着：提醒的状态、到期时间、"
        "谁正在处理它，全部以数据库为准。没有内存计时器——因为内存状态在进程崩溃时全部丢失。"
        "查询条件 `status = PENDING AND remind_at <= now` 就是 Worker 的「待办清单」。",
        "Polling means repeatedly asking: \"is there due work?\" The database being the single source of "
        "truth means reminder status, due time, and who is handling it are all recorded in the database. "
        "No in-memory timers — because memory dies with the process. The query "
        "status = PENDING AND remind_at <= now is the worker's to-do list.",
        [
            ("轮询 — Polling", "Repeated queries for ready work"),
            ("事实来源 — Source of truth", "The place where the real state lives"),
            ("待办清单 — To-do list", "The set of due records"),
        ],
        "process_due_reminders_once 用 SQLAlchemy 查询 PENDING 且 remind_at <= now 的记录。",
        "process_due_reminders_once queries records with status PENDING and remind_at <= now.",
        (
            "“The database is the single source of truth, so restarts lose nothing and two workers see the same state.”",
            "「数据库是唯一事实来源，重启不丢状态，两个 Worker 看到同一份状态。」",
        ),
    )

    _knowledge_point(
        doc,
        "Reminder 状态机：五种状态",
        "The Reminder state machine: five states",
        "Reminder 有自己的状态机：PENDING（等待）、CLAIMED（已被领取）、DELIVERED（已投递）、CANCELLED（已取消）、"
        "FAILED（失败）。状态由数据库字段驱动，Worker 的每次操作都是带条件的更新——比如「只有 PENDING 且到期的"
        "记录才能被领取」。条件更新就是并发安全的关键。",
        "Reminders have their own state machine: PENDING (waiting), CLAIMED (claimed), DELIVERED (delivered), "
        "CANCELLED (cancelled), and FAILED. The state is driven by database fields, and every worker "
        "operation is a conditional update — for example, only PENDING and due records may be claimed. "
        "Conditional updates are the key to concurrency safety.",
        [
            ("状态机 — State machine", "Fixed states and transitions"),
            ("终态 — Terminal state", "A state that never changes again"),
            ("条件更新 — Conditional update", "An update that only applies when the condition holds"),
        ],
        "app/models/reminder.py 的 ReminderStatus 枚举与 reminder_service 的条件 UPDATE 语句。",
        "The ReminderStatus enum in app/models/reminder.py and the conditional UPDATE statements in "
        "reminder_service.",
        (
            "“Every worker operation is a conditional update, so the state machine is safe under concurrency.”",
            "「Worker 的每个操作都是条件更新，状态机在并发下也是安全的。」",
        ),
    )

    _knowledge_point(
        doc,
        "Notification：提醒的最终呈现",
        "Notification: the final presentation of a reminder",
        "Notification 是用户真正看到的东西：一条带标题、正文、时间的应用内通知。它与 Reminder 一一对应——"
        "一个 Reminder 至多一条 Notification（唯一约束）。通知可以标记已读，已读状态幂等：重复标记不改变结果。",
        "A notification is what the user actually sees: an in-app message with a title, body, and time. It "
        "maps one-to-one to a reminder — at most one notification per reminder (unique constraint). "
        "Notifications can be marked read, and marking is idempotent: repeating it changes nothing.",
        [
            ("通知 — Notification", "The user-visible message"),
            ("一一对应 — One-to-one", "Exactly one notification per reminder"),
            ("已读 — Read state", "Whether the user has seen it"),
        ],
        "app/models/reminder.py 的 Notification 模型（reminder_id 唯一）；POST /api/notifications/{id}/read。",
        "The Notification model in app/models/reminder.py (unique reminder_id); POST /api/notifications/{id}/read.",
        (
            "“A reminder delivers at most one notification, enforced by a unique constraint on reminder_id.”",
            "「一个提醒至多投递一条通知，由 reminder_id 上的唯一约束保证。」",
        ),
    )

    _knowledge_point(
        doc,
        "幂等性：重复执行等于执行一次",
        "Idempotency: repeating equals doing once",
        "幂等（idempotent）操作是指：执行一次和执行多次效果相同。标记已读是幂等的；投递通知也必须是幂等的——"
        "因为 Worker 可能重复运行、重复扫描。JARVIS 的幂等策略：投递时靠唯一约束去重（第二次插入冲突 → 视为已投递），"
        "Worker 重复运行不会产生第二条通知。",
        "An idempotent operation has the same effect whether it runs once or many times. Marking read is "
        "idempotent; delivering a notification must be too — because workers may run repeatedly and "
        "re-scan records. JARVIS's strategy: the unique constraint deduplicates (a second insert conflicts "
        "and is treated as already delivered), so repeated worker runs never create a second notification.",
        [
            ("幂等 — Idempotent", "Repeating has the same effect as doing once"),
            ("去重 — Deduplication", "Preventing duplicates from repeating work"),
            ("重复执行 — Re-execution", "Running the same operation again"),
        ],
        "reminder_service._deliver 捕获唯一冲突并视为已投递；test_worker_idempotent_no_duplicate_notification 验证。",
        "_deliver in reminder_service catches the unique conflict and treats it as delivered; "
        "test_worker_idempotent_no_duplicate_notification verifies it.",
        (
            "“Delivery is idempotent: the unique constraint deduplicates, so rerunning the worker never duplicates a notification.”",
            "「投递是幂等的：唯一约束去重，重复运行 Worker 不会产生重复通知。」",
        ),
    )

    _knowledge_point(
        doc,
        "exactly-once 为什么困难",
        "Why exactly-once is hard",
        "理想的投递语义是 exactly-once：恰好一次，不多不少。现实中很难做到，因为「发送」和「确认发送成功」之间"
        "可能崩溃：通知已写入但 Worker 在提交前崩溃，重试就会重复。JARVIS 诚实实现 at-least-once + 去重："
        "可能尝试多次，但唯一约束保证用户只看到一条通知。我们明确不承诺 exactly-once。",
        "The ideal delivery semantics is exactly-once: precisely one delivery, no more, no less. In practice "
        "it is hard, because a crash can happen between sending and confirming the send: the notification "
        "is written but the worker dies before committing, and a retry duplicates it. JARVIS honestly "
        "implements at-least-once plus deduplication: attempts may repeat, but the unique constraint "
        "guarantees the user sees exactly one notification. We do not claim exactly-once.",
        [
            ("恰好一次 — Exactly-once", "Exactly one delivery, never more"),
            ("至少一次 — At-least-once", "At least one delivery, possibly more"),
            ("去重 — Dedup", "Making at-least-once look like once"),
        ],
        "README 的可靠性说明与 Notification.reminder_id 唯一约束的注释。",
        "The reliability notes in the README and the comment on the Notification.reminder_id unique constraint.",
        (
            "“We implement at-least-once with deduplication rather than claiming exactly-once, because exactly-once is not realistically achievable here.”",
            "「我们实现至少一次加去重，而不是声称恰好一次——恰好一次在这里不现实。」",
        ),
    )

    _knowledge_point(
        doc,
        "唯一约束：数据库层面的最终防线",
        "Unique constraints: the database's final line of defense",
        "代码可能出错，逻辑可能有漏洞，但数据库约束不会说谎。Notification.reminder_id 的唯一约束意味着："
        "无论多少 Worker、多少重试，数据库层面都不可能出现同一个 Reminder 的两条通知。这是幂等投递的最终保障——"
        "程序逻辑之上的最后一层。",
        "Code can be buggy and logic can have holes, but database constraints do not lie. The unique "
        "constraint on Notification.reminder_id means no matter how many workers or retries, the database "
        "itself cannot hold two notifications for one reminder. That is the final guarantee of idempotent "
        "delivery — the last layer above program logic.",
        [
            ("约束 — Constraint", "A rule the database enforces"),
            ("最终防线 — Last line of defense", "The guarantee that cannot be bypassed"),
            ("冲突 — Conflict", "An insert that violates the constraint"),
        ],
        "app/models/reminder.py 中 Notification.reminder_id 的 unique=True；迁移 fa94512e0868 的 ix_notifications_reminder_id。",
        "unique=True on Notification.reminder_id in app/models/reminder.py; the index in migration fa94512e0868.",
        (
            "“The unique constraint is our final guarantee: even a buggy retry cannot create a duplicate notification.”",
            "「唯一约束是最终保障：即使重试逻辑有 bug 也无法产生重复通知。」",
        ),
    )

    _knowledge_point(
        doc,
        "数据库事务：领取与投递的原子性",
        "Database transactions: atomicity of claiming and delivery",
        "Worker 的领取（状态更新）和投递（插入通知 + 状态更新）都是数据库事务。领取事务失败则提醒未被领取；"
        "投递事务失败则整体回滚——通知不会出现一半。JARVIS 把领取单独提交（这样崩溃后靠 lease 恢复），"
        "把通知与 DELIVERED 放在同一事务（要么都有，要么都没有）。",
        "The worker's claim (status update) and delivery (insert notification plus status update) are "
        "database transactions. A failed claim leaves the reminder unclaimed; a failed delivery rolls back "
        "as a whole — a half notification never appears. JARVIS commits the claim separately (so a crash "
        "recovers via the lease) and puts the notification and DELIVERED in one transaction (both or neither).",
        [
            ("事务 — Transaction", "A group of writes that commit or roll back together"),
            ("提交 — Commit", "Making the writes permanent"),
            ("回滚 — Rollback", "Undoing all writes of the transaction"),
        ],
        "reminder_service._claim_and_deliver：领取先 commit，投递（Notification + DELIVERED）同一事务。",
        "reminder_service._claim_and_deliver: the claim commits first; delivery (notification plus DELIVERED) is one transaction.",
        (
            "“The claim commits first, and the notification with the delivered state commit atomically.”",
            "「领取先提交，通知与已投递状态一起原子提交。」",
        ),
    )

    _knowledge_point(
        doc,
        "Worker 竞争与 lease：谁拿到了就是谁的",
        "Worker competition and leases: first claim wins",
        "两个 Worker 同时看到同一条到期提醒时，只有一个人能拿到它。原子领取用「条件 UPDATE」实现："
        "`UPDATE ... WHERE id=? AND status='PENDING' AND remind_at<=now`——SQLite 单写者串行化保证只有一个 "
        "UPDATE 影响一行。领取后设置 lease（租约）：一个截止时间，Worker 崩溃后租约到期，其他 Worker 才能重新领取。",
        "When two workers see the same due reminder, only one can get it. Atomic claiming is a conditional "
        "UPDATE: UPDATE ... WHERE id=? AND status='PENDING' AND remind_at<=now — SQLite's single-writer "
        "serialization guarantees only one UPDATE affects a row. After claiming, a lease is set: a deadline "
        "after which another worker may reclaim if this one crashed.",
        [
            ("竞争 — Competition", "Multiple workers racing for the same record"),
            ("租约 — Lease", "A temporary exclusive claim with a deadline"),
            ("条件更新 — Conditional update", "The atomic claim operation"),
            ("单写者 — Single writer", "SQLite's serialized writes"),
        ],
        "reminder_service._claim_and_deliver 的条件 UPDATE 与 lease_expires_at 字段；竞争测试验证通知唯一。",
        "The conditional UPDATE and lease_expires_at field in reminder_service._claim_and_deliver; the "
        "competition test verifies a single notification.",
        (
            "“A conditional update makes the claim atomic: exactly one worker wins, and the lease lets crashed workers be recovered.”",
            "「条件更新让领取原子化：恰好一个 Worker 获胜，租约让崩溃的 Worker 可以被恢复。」",
        ),
    )

    _knowledge_point(
        doc,
        "崩溃恢复与重试上限",
        "Crash recovery and the retry ceiling",
        "Worker 领取后可能在任何时刻崩溃。恢复机制：租约到期后其他 Worker 重新领取，attempt_count 继续累加。"
        "但重试不能无限：达到 REMINDER_MAX_ATTEMPTS 后 Reminder 变为 FAILED（毒消息处理）。"
        "可重试错误（如数据库暂时不可用）保持 CLAIMED 等租约；永久数据错误直接 FAILED。",
        "A worker may crash at any moment after claiming. Recovery: after the lease expires, another worker "
        "reclaims and attempt_count keeps growing. But retries cannot be infinite: at REMINDER_MAX_ATTEMPTS "
        "the reminder becomes FAILED (poison-message handling). Retryable errors (like a temporarily "
        "unavailable database) stay CLAIMED until the lease expires; permanent data errors fail immediately.",
        [
            ("崩溃恢复 — Crash recovery", "Reclaiming work after a crash"),
            ("尝试次数 — Attempt count", "How many times delivery was tried"),
            ("失败上限 — Failure ceiling", "MAX_ATTEMPTS turns the record into FAILED"),
            ("毒消息 — Poison message", "A record that keeps failing and must be quarantined"),
        ],
        "reminder_service 的 MAX_ATTEMPTS 分支与 FAILED 状态；test_attempt_count_and_failed_after_max 验证。",
        "The MAX_ATTEMPTS branch and FAILED state in reminder_service; test_attempt_count_and_failed_after_max verifies it.",
        (
            "“Retries are bounded: after MAX_ATTEMPTS the reminder fails permanently instead of retrying forever.”",
            "「重试有上限：达到最大尝试次数后提醒永久失败，而不是无限重试。」",
        ),
    )

    _knowledge_point(
        doc,
        "Fake Clock：测试里的时间旅行",
        "Fake Clock: time travel in tests",
        "测试提醒到期不能真的等一小时——那既慢又不可靠。Fake Clock 是 Phase 2 引入的可注入时钟：测试把时钟固定"
        "到任意时刻，把 remind_at 改成过去就能立即「到期」。所有 Phase 3 测试用 Fake Clock，禁止 sleep。"
        "Worker 入口还支持 --once --now 参数，真实进程也能用固定时间单次处理。",
        "Testing reminder expiry must not actually wait an hour — slow and flaky. The Fake Clock, introduced "
        "in Phase 2, is an injectable clock: tests freeze time at any instant, and setting remind_at in the "
        "past makes a reminder due immediately. Every Phase 3 test uses a Fake Clock; sleep is forbidden. "
        "The worker entry also supports --once --now, so even a real process can run once with a fixed time.",
        [
            ("时间旅行 — Time travel", "Moving the clock forward in tests"),
            ("冻结 — Freeze", "Fixing the clock at one instant"),
            ("禁止 sleep — No sleeping", "Tests never wait for real time"),
        ],
        "tests/conftest.py 的 FIXED_NOW 与 FixedClock 注入；worker 的 --now 参数。",
        "FIXED_NOW and the FixedClock injection in tests/conftest.py; the --now flag of the worker.",
        (
            "“We freeze time with a Fake Clock, so expiry tests run instantly and deterministically.”",
            "「我们用 Fake Clock 冻结时间，到期测试瞬时且确定。」",
        ),
    )

    _knowledge_point(
        doc,
        "graceful shutdown：给 Worker 一个体面的退场",
        "Graceful shutdown: letting the worker leave politely",
        "Worker 是无限循环，怎么停止它？粗暴方式是直接杀进程——可能正好杀在投递事务中间。graceful shutdown"
        "（优雅退出）是：收到停止信号（SIGINT/SIGTERM）后，停止领取新工作，完成当前工作，然后退出。"
        "JARVIS 的 Worker 捕获信号后在下一次循环检查时退出，保证正在进行的投递不被截断。",
        "A worker is an infinite loop — how does it stop? The crude way is killing the process, possibly "
        "right in the middle of a delivery transaction. Graceful shutdown means: on a stop signal "
        "(SIGINT/SIGTERM), stop claiming new work, finish the current work, then exit. JARVIS's worker "
        "catches the signal and exits at the next loop check, so an in-flight delivery is never truncated.",
        [
            ("优雅退出 — Graceful shutdown", "Finishing current work before exiting"),
            ("停止信号 — Stop signal", "SIGINT or SIGTERM"),
            ("飞行中 — In-flight", "Work that is currently being processed"),
        ],
        "app/workers/reminder_worker.py 的 run_forever：signal handler 设置停止标志，循环检查后退出。",
        "run_forever in app/workers/reminder_worker.py: a signal handler sets a stop flag and the loop exits after checking it.",
        (
            "“On SIGTERM the worker finishes the current delivery and exits cleanly instead of being killed mid-transaction.”",
            "「收到 SIGTERM 时 Worker 完成当前投递再退出，而不是在事务中途被杀。」",
        ),
    )

    _knowledge_point(
        doc,
        "为什么不让 Worker 住在 FastAPI 里",
        "Why the worker does not live inside FastAPI",
        "开发时 uvicorn --reload 会在文件变化时重启进程——如果 Worker 循环放在 FastAPI 生命周期里，"
        "reload 会启动多个 Worker 实例，多个实例同时轮询同一批提醒，虽然幂等投递兜底，但会产生无谓竞争与日志噪音。"
        "把 Worker 独立成进程（python -m app.workers.reminder_worker）后，开发、测试、部署都能精确控制实例数量。",
        "In development, uvicorn --reload restarts the process on file changes — if the worker loop lived "
        "inside FastAPI's lifecycle, reload would spawn multiple worker instances polling the same records. "
        "Idempotent delivery would still protect the data, but the pointless competition and log noise "
        "remain. Running the worker as a separate process (python -m app.workers.reminder_worker) gives "
        "precise control over instance count in dev, test, and production.",
        [
            ("热重载 — Hot reload", "Auto-restarting on file changes"),
            ("实例数量 — Instance count", "How many copies of a process run"),
            ("独立进程 — Separate process", "Its own process, its own lifecycle"),
        ],
        "README 的「Worker 运行方式」与 app/workers/reminder_worker.py 的独立入口。",
        "The Worker section of the README and the standalone entry in app/workers/reminder_worker.py.",
        (
            "“The worker runs as a separate process because hot reload would otherwise spawn multiple polling instances.”",
            "「Worker 独立成进程，因为热重载否则会启动多个轮询实例。」",
        ),
    )

    _knowledge_point(
        doc,
        "Task、Reminder、Notification 的关系",
        "The relationship between Task, Reminder, and Notification",
        "三者是层层递进的关系：Task 是业务对象（用户的任务），Reminder 是任务的一个可选的提醒计划（有 due_at 才存在），"
        "Notification 是提醒投递后用户看到的结果。一个 Task 最多一个活动 Reminder（部分唯一索引），一个 Reminder "
        "至多一条 Notification（唯一约束）。它们通过外键连接，历史永不删除。",
        "The three are layered: a Task is a business object (the user's task), a Reminder is an optional "
        "scheduling plan for the task (only when due_at exists), and a Notification is what the user sees "
        "after delivery. A task has at most one active reminder (partial unique index), and a reminder has "
        "at most one notification (unique constraint). They are linked by foreign keys, and history is "
        "never deleted.",
        [
            ("外键 — Foreign key", "A column linking to another table"),
            ("可选 — Optional", "A reminder only exists when due_at does"),
            ("历史保留 — History kept", "Delivered records are never deleted"),
        ],
        "reminders.task_id、notifications.reminder_id/task_id 外键；task_proposals 到 tasks 的连接。",
        "The foreign keys reminders.task_id and notifications.reminder_id/task_id; the task_proposals-to-tasks link.",
        (
            "“A task can have many reminders over its life, but at most one active one, and each delivered reminder maps to exactly one notification.”",
            "「任务一生可以有多个提醒记录，但同一时刻至多一个活动提醒；每个已投递提醒对应恰好一条通知。」",
        ),
    )

    _knowledge_point(
        doc,
        "Proposal 为什么不能直接创建 Reminder",
        "Why a Proposal cannot create a Reminder directly",
        "Proposal 创建的任务永远是 DRAFT。DRAFT 是「尚未生效」的状态——它还没有被用户确认，自然不能安排提醒。"
        "提醒的副作用被刻意绑定在 confirm 这个生效动作上：只有用户确认后，同一事务才创建 Reminder。"
        "这延续了 Phase 0 的契约：创建无副作用，确认是唯一生效入口。",
        "A proposal always creates a DRAFT task. DRAFT means not yet active — the user has not confirmed it, "
        "so no reminder may be scheduled. The reminder side effect is deliberately attached to the confirm "
        "activation: only after the user confirms does the same transaction create a reminder. This "
        "continues the Phase 0 contract: creation is side-effect-free, and confirmation is the only "
        "activation entry.",
        [
            ("生效 — Activation", "The moment a draft becomes real"),
            ("副作用绑定 — Side-effect binding", "Attaching effects to the right step"),
            ("契约延续 — Contract continuity", "Keeping creation side-effect-free"),
        ],
        "proposal_service 只调用 create_task（DRAFT）；reminder 由 confirm 事务创建（schedule_on_confirm）。",
        "proposal_service only calls create_task (DRAFT); reminders are created by the confirm transaction "
        "via schedule_on_confirm.",
        (
            "“Proposals create drafts only; reminders appear only after the user's confirm call, in the same transaction.”",
            "「建议只创建草稿；提醒只在用户确认调用之后、在同一事务中出现。」",
        ),
    )

    # ---------------- 核心代码讲解 ----------------
    doc.add_heading("核心代码讲解", "Core code explained", level=1)
    doc.add_pair(
        "下面五段代码是 Phase 3 的核心实现。请先阅读真实文件，再对照讲解。",
        "The five snippets below are the heart of Phase 3. Read the real files first, then follow the explanations.",
    )

    doc.add_code_explain(
        {
            "location": "backend/app/services/task_service.py — confirm_task 事务集成",
            "code": '''def confirm_task(db: Session, task_id: int) -> Task:
    """确认草稿：DRAFT -> CONFIRMED。

    同一事务内：若存在 due_at，创建 PENDING Reminder（remind_at=due_at）；
    无 due_at 不创建。重复 confirm 由状态机 409 拒绝。
    """
    task = _transition(db, task_id, "confirm", commit=False)
    reminder_service.schedule_on_confirm(db, task)
    db.commit()
    db.refresh(task)
    return task''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "确认任务的完整事务：状态转换与提醒创建在同一个数据库事务里。状态机检查通过后才创建提醒，"
                    "任何一步失败整体回滚——不会出现「任务已确认但提醒丢失」或反之。",
                    "en": "The full confirmation transaction: the state change and the reminder creation share "
                    "one database transaction. The reminder is created only after the state-machine check "
                    "passes, and any failure rolls back everything — no task-without-reminder or "
                    "reminder-without-task state.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("_transition(commit=False) 只改状态不提交。", "_transition(commit=False) changes the status without committing."),
                        ("schedule_on_confirm 在同一会话创建 PENDING Reminder（无 due_at 返回 None）。", "schedule_on_confirm creates a PENDING reminder in the same session (None when due_at is absent)."),
                        ("一次 commit 同时固化状态与提醒。", "One commit finalizes both the status and the reminder."),
                    ],
                    "en_intro": "Change the status without committing, create the reminder in the same "
                    "session, then commit once.",
                },
                {
                    "kind": "why",
                    "cn": "副作用与状态转换同事务是「一致性」的根基：提醒的存在与否永远与任务状态一致。"
                    "重复 confirm 被状态机 409 拦截，部分唯一索引是第二道防重。",
                    "en": "Putting the side effect and the state change in one transaction is the root of "
                    "consistency: whether a reminder exists always matches the task status. Duplicate "
                    "confirms are blocked by the 409 state check, and the partial unique index is a second "
                    "guard.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是在路由层分别调用两个服务（先确认再单独创建提醒）——中间崩溃会留下不一致；"
                    "或者忘记 commit=False 参数，让状态先提交、提醒后提交。",
                    "en": "A common mistake is calling two services separately in the route (confirm, then "
                    "create a reminder separately) — a crash in between leaves inconsistency; another is "
                    "forgetting commit=False so the status commits before the reminder.",
                },
            ],
        }
    )

    doc.add_code_explain(
        {
            "location": "backend/app/services/reminder_service.py — process_due_reminders_once 轮询",
            "code": '''    now = _utc_naive(clock.now())
    stats = WorkerStats()

    candidates = (
        db.query(Reminder)
        .filter(
            or_(
                and_(Reminder.status == PENDING, Reminder.remind_at <= now),
                and_(Reminder.status == CLAIMED, Reminder.lease_expires_at < now),
            )
        )
        .order_by(Reminder.remind_at.asc())
        .limit(batch_size)
        .all()
    )
    stats.scanned = len(candidates)''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "单次轮询的候选查询：找「PENDING 且到期」或「CLAIMED 且租约过期」的提醒，按到期时间排序，"
                    "每批最多 batch_size 条。返回统计信息供调用方记录。",
                    "en": "The candidate query of one poll: find reminders that are PENDING and due, or CLAIMED "
                    "with an expired lease, ordered by due time, capped at batch_size per run. Stats are "
                    "returned for the caller to log.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("时钟注入：now 来自 Clock 而非 datetime.now()。", "The clock is injected: now comes from Clock, not datetime.now()."),
                        ("两个条件覆盖初次领取与崩溃恢复。", "Two conditions cover first claiming and crash recovery."),
                        ("_utc_naive 统一为 naive UTC，与 SQLite 存储格式一致。", "_utc_naive normalizes to naive UTC, matching SQLite's storage format."),
                        ("limit(batch_size) 控制单批工作量。", "limit(batch_size) bounds the work of one pass."),
                    ],
                    "en_intro": "An injected clock provides now; the query covers due PENDING and "
                    "lease-expired CLAIMED records; naive UTC keeps comparisons consistent; batch_size "
                    "bounds each pass.",
                },
                {
                    "kind": "why",
                    "cn": "轮询查询本身就是「待办清单」：数据库是唯一事实来源，没有内存状态。"
                    "租约过期条件让崩溃的 Worker 留下的记录能被重新处理。",
                    "en": "The poll query is the to-do list itself: the database is the single source of "
                    "truth, with no in-memory state. The expired-lease condition lets records left by "
                    "crashed workers be processed again.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是直接 datetime.now()（无法测试）；或用 aware datetime 绑定 SQLite 参数"
                    "（与读回的 naive 值比较会抛 TypeError）；或忘记 limit 导致单批处理无限大。",
                    "en": "Common mistakes: calling datetime.now() directly (untestable); binding aware "
                    "datetimes into SQLite comparisons with naive values read back (TypeError); or "
                    "forgetting the limit so one pass processes unbounded work.",
                },
            ],
        }
    )

    doc.add_code_explain(
        {
            "location": "backend/app/services/reminder_service.py — 原子领取",
            "code": '''    result = db.execute(
        update(Reminder)
        .where(
            Reminder.id == reminder.id,
            or_(
                and_(Reminder.status == PENDING, Reminder.remind_at <= now),
                and_(Reminder.status == CLAIMED, Reminder.lease_expires_at < now),
            ),
        )
        .values(
            status=CLAIMED,
            claimed_at=now,
            lease_expires_at=lease_expires,
            attempt_count=new_attempt,
        )
    )
    db.commit()  # 领取固化（领取即计数：失败也会增加 attempt_count）
    if result.rowcount == 0:
        return "skipped"  # 被其他 Worker 领取或状态已变化''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "原子领取：一条带条件的 UPDATE。条件（PENDING+到期，或 CLAIMED+租约过期）写进 SQL 本身，"
                    "两个 Worker 同时执行时只有一个能更新到这一行。",
                    "en": "Atomic claiming: a single conditional UPDATE. The conditions (PENDING and due, or "
                    "CLAIMED with expired lease) are part of the SQL itself, so when two workers execute "
                    "concurrently, only one updates the row.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("条件在 UPDATE 的 WHERE 里——不是先 SELECT 再 UPDATE。", "The conditions are in the UPDATE's WHERE — not select-then-update."),
                        ("领取即计数：attempt_count 在领取时加一。", "Claiming counts: attempt_count increments at claim time."),
                        ("提交后检查 rowcount：0 表示竞争失败（skipped）。", "After commit, rowcount 0 means the race was lost (skipped)."),
                    ],
                    "en_intro": "The claim is one conditional UPDATE; attempts count at claim time; rowcount "
                    "distinguishes the winner from the skipped.",
                },
                {
                    "kind": "why",
                    "cn": "SELECT 后 UPDATE 的「先查后改」模式在并发下会竞态：两个 Worker 都读到 PENDING，都去更新。"
                    "把条件放进 UPDATE 本身，数据库保证原子性——这是 SQLite 单写者下可靠且简单的安全方案。",
                    "en": "The select-then-update pattern races under concurrency: both workers read PENDING "
                    "and both try to update. Putting the condition inside the UPDATE lets the database "
                    "guarantee atomicity — a reliable and simple approach under SQLite's single writer.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是先 SELECT 再 UPDATE（竞态）；或领取与投递放在一个超长事务里（崩溃时整批回滚，"
                    "租约机制失效）；或不检查 rowcount，把竞争失败也当作领取成功。",
                    "en": "Common mistakes: select-then-update (a race); one giant transaction spanning claim "
                    "and delivery (a crash rolls back the whole batch and defeats the lease); or ignoring "
                    "rowcount and treating a lost race as a successful claim.",
                },
            ],
        }
    )

    doc.add_code_explain(
        {
            "location": "backend/app/services/reminder_service.py — 幂等投递",
            "code": '''    try:
        notification = Notification(
            reminder_id=reminder.id,
            task_id=reminder.task_id,
            type=NotificationType.REMINDER_DUE.value,
            title=f"任务提醒：{title}",
            body=f"任务「{title}」已到截止时间。",
        )
        db.add(notification)
        reminder.status = DELIVERED
        reminder.delivered_at = now
        reminder.last_error_code = None
        db.commit()
        return "delivered"
    except IntegrityError as exc:
        db.rollback()
        # 唯一冲突：reminder_id 已有 Notification → 幂等，视为已投递
        existing = (
            db.query(Notification).filter(Notification.reminder_id == reminder.id).first()
        )
        if existing is not None:
            db.execute(
                update(Reminder)
                .where(Reminder.id == reminder.id)
                .values(status=DELIVERED, delivered_at=now)
            )
            db.commit()
            return "delivered"''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "幂等投递：Notification 与 DELIVERED 在同一事务。若唯一约束冲突（说明之前已投递过），"
                    "回滚后把 Reminder 直接标记为 DELIVERED——重复执行的结果与执行一次相同。",
                    "en": "Idempotent delivery: the notification and DELIVERED commit in one transaction. If "
                    "the unique constraint conflicts (delivery already happened), the rollback is followed "
                    "by marking the reminder DELIVERED — repeating produces the same result as doing it once.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("插入 Notification 并标记 DELIVERED，一起提交。", "Insert the notification and mark DELIVERED, committing together."),
                        ("IntegrityError 说明有冲突。", "An IntegrityError means a conflict."),
                        ("查一次：通知已存在 → 视为已投递（幂等路径）。", "Check once: a notification exists → treat as delivered (idempotent path)."),
                        ("其他冲突（外键等）→ 永久失败路径。", "Other conflicts (foreign keys, etc.) take the permanent-failure path."),
                    ],
                    "en_intro": "Insert and mark delivered in one transaction; on a unique conflict, confirm "
                    "a notification already exists and mark delivered anyway.",
                },
                {
                    "kind": "why",
                    "cn": "幂等性的核心是「第二次执行必须无害」。唯一约束把「检查是否已投递」交给数据库——比应用层"
                    "先查再插更可靠（查与插之间没有竞态窗口）。",
                    "en": "The essence of idempotency is that a second run must be harmless. The unique "
                    "constraint delegates the already-delivered check to the database — more reliable than "
                    "an application-level check-then-insert, which has a race window.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是应用层先查「有没有通知」再插入——两个 Worker 同时查都为空，双双插入，"
                    "唯一约束兜底但会产生一次多余的异常路径；或把唯一冲突当成普通错误返回 500，而不是幂等处理。",
                    "en": "A common mistake is checking-then-inserting at the application layer — two workers "
                    "both see nothing and both insert, forcing the unique constraint to clean up; another is "
                    "treating the unique conflict as a plain error instead of the idempotent path.",
                },
            ],
        }
    )

    doc.add_code_explain(
        {
            "location": "backend/app/workers/reminder_worker.py — 优雅退出与单次模式",
            "code": '''def run_forever(*, poll_interval: int) -> None:
    """无限轮询，支持 SIGINT/SIGTERM 优雅退出。"""
    stop = {"flag": False}

    def _handle(signum, frame):
        logger.info("reminder_worker.shutdown signal=%s", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    while not stop["flag"]:
        db = SessionLocal()
        try:
            stats = process_due_reminders_once(...)
            logger.info("reminder_worker.tick %s", stats)
        except Exception:
            logger.exception("reminder_worker.error")
        finally:
            db.close()
        for _ in range(poll_interval):
            if stop["flag"]:
                break
            time.sleep(1)
    logger.info("reminder_worker.stopped")''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "Worker 主循环：每轮处理一次到期提醒，然后按轮询间隔休眠；收到停止信号后在下一次循环检查时"
                    "退出。单轮异常被捕获记录，不会让整个 Worker 崩溃。",
                    "en": "The worker main loop: process due reminders once per round, sleep for the poll "
                    "interval, and exit at the next loop check after a stop signal. Per-round exceptions "
                    "are caught and logged so a single failure never kills the worker.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("注册 SIGINT/SIGTERM 处理器，设置停止标志。", "Register SIGINT/SIGTERM handlers that set a stop flag."),
                        ("每轮新建数据库会话，用完关闭。", "Each round opens a fresh session and closes it in finally."),
                        ("异常只记录不退出——Worker 必须存活。", "Exceptions are logged, not fatal — the worker must survive."),
                        ("休眠按秒检查停止标志，实现快速优雅退出。", "The sleep loop checks the stop flag per second for a quick graceful exit."),
                    ],
                    "en_intro": "Signal handlers set a stop flag; each round processes due reminders in a "
                    "fresh session; exceptions are logged; the sleep loop exits early on the stop flag.",
                },
                {
                    "kind": "why",
                    "cn": "Worker 独立于 API 进程（避免 reload 多实例）；单轮容错保证「一个坏记录打不死 Worker」；"
                    "优雅退出让正在进行的投递事务有始有终。",
                    "en": "The worker is separate from the API process (avoiding reload multi-instances); "
                    "per-round fault tolerance keeps one bad record from killing the worker; graceful "
                    "shutdown lets in-flight delivery transactions finish.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是把 Worker 循环放进 FastAPI lifespan（reload 多实例）；或单轮异常直接抛出"
                    "（一个毒消息让整个 Worker 退出，所有后续提醒都停摆）。",
                    "en": "Common mistakes: putting the worker loop inside FastAPI's lifespan (reload spawns "
                    "multiple instances); or letting a per-round exception propagate (one poison message "
                    "stops the whole worker and every later reminder).",
                },
            ],
        }
    )

    # ---------------- 深入话题：可靠性细节 ----------------
    doc.add_heading("深入话题：可靠性细节", "Deep dive: reliability details", level=1)
    doc.add_pair(
        "前面的知识点回答了「怎么运转」，这一节回答「为什么这样设计」：SQLite 外键为什么需要手动开启、"
        "部分唯一索引如何防重、lease 边界的数学细节、CLAIMED 期间的用户操作策略、列表分页的确定性、"
        "以及未来迁移 PostgreSQL 的方向。这些细节正是严格代码审查会逐一核对的点。",
        "The earlier knowledge points answered how it works; this section answers why it is designed this "
        "way: why SQLite foreign keys must be enabled manually, how the partial unique index prevents "
        "duplicates, the exact lease boundary, the policy for user actions while a reminder is CLAIMED, "
        "deterministic pagination, and the PostgreSQL migration direction. These details are exactly what "
        "a strict code review checks one by one.",
    )

    _knowledge_point(
        doc,
        "SQLite 外键：默认不生效的 PRAGMA",
        "SQLite foreign keys: the PRAGMA that must be enabled",
        "SQLite 出于向后兼容默认不启用外键约束——外键列可以引用不存在的行，DELETE CASCADE 也不会触发。"
        "create_all 会生成 FOREIGN KEY 子句，但如果连接不执行 PRAGMA foreign_keys=ON，这些约束只是装饰。"
        "JARVIS 在 engine 的 connect 事件里统一开启，测试夹具的引擎也挂载同一个监听器——否则测试里外键"
        "不生效，CASCADE 与拒绝插入的行为根本测不到，测试与生产就会分叉。",
        "For backward compatibility, SQLite does not enforce foreign keys by default: a column may reference "
        "a missing row and DELETE CASCADE never fires. create_all emits FOREIGN KEY clauses, but without "
        "executing PRAGMA foreign_keys=ON per connection they are decoration only. JARVIS enables the pragma "
        "on the connect event of every engine, including test fixtures — otherwise tests cannot observe "
        "CASCADE or rejected inserts, and tests diverge from production.",
        [
            ("外键约束 — Foreign key constraint", "Integrity rule linking columns across tables"),
            ("级联删除 — Cascade delete", "Deleting a row deletes its dependents"),
            ("事件监听器 — Event listener", "Hook that runs on every connection"),
        ],
        "app/core/database.py 的 _set_sqlite_pragma 监听 connect 事件；tests/conftest.py 的 client 与 worker_env 引擎挂载同一函数。",
        "_set_sqlite_pragma in app/core/database.py listens on connect; the client and worker_env engines in "
        "tests/conftest.py attach the same function.",
        (
            "“Foreign keys must be enabled per connection with PRAGMA foreign_keys=ON.”",
            "「外键必须通过 PRAGMA foreign_keys=ON 在每个连接上开启。」",
        ),
    )

    _knowledge_point(
        doc,
        "部分唯一索引：同一任务最多一个活动提醒",
        "Partial unique index: at most one active reminder per task",
        "普通唯一索引要求整列唯一；部分唯一索引（partial unique index）只在 WHERE 条件命中的行上强制唯一。"
        "uq_reminders_active_task 对 task_id 唯一，但仅当 status IN (PENDING, CLAIMED)——终态（DELIVERED/"
        "CANCELLED/FAILED）允许同一任务的多个历史记录并存。重复 confirm 同一任务时，第二个 PENDING 插入撞"
        "唯一约束 → IntegrityError → 整体回滚 → 表现为幂等成功。这是「同一任务最多一个未结束提醒」的最终防重，"
        "位于数据库层，应用代码怎么改都不会绕过。",
        "A plain unique index requires uniqueness over the whole column; a partial unique index enforces it "
        "only on rows matching a WHERE clause. uq_reminders_active_task is unique on task_id only while "
        "status IN (PENDING, CLAIMED) — terminal states (DELIVERED/CANCELLED/FAILED) may coexist as history "
        "for the same task. Confirming the same task again inserts a second PENDING, hits the unique "
        "constraint, raises IntegrityError, and rolls back — surfacing as idempotent success. This is the "
        "final guarantee that a task has at most one unfinished reminder, enforced in the database where no "
        "application code can bypass it.",
        [
            ("部分索引 — Partial index", "Index with a WHERE clause"),
            ("活动提醒 — Active reminder", "PENDING or CLAIMED only"),
            ("防重 — Dedup guard", "The database rejects a second active reminder"),
        ],
        "backend/app/models/reminder.py 的 Index(\"uq_reminders_active_task\", unique=True, sqlite_where=...)。",
        "The Index(\"uq_reminders_active_task\", unique=True, sqlite_where=...) in backend/app/models/reminder.py.",
        (
            "“A partial unique index enforces uniqueness only on matching rows.”",
            "「部分唯一索引只在匹配的行上强制唯一。」",
        ),
    )

    _knowledge_point(
        doc,
        "lease 边界：小于才可领，等于不可领",
        "Lease boundary: strictly-less-than, never equal",
        "Worker 的候选查询有两组条件，边界非常精确：PENDING 且 remind_at <= now（到期即处理，等号允许）；"
        "CLAIMED 且 lease_expires_at < now（严格小于——lease 恰好到期的记录不在这一轮被抢）。为什么？如果"
        "等号也可领，另一个 Worker 可能正在提交这条记录的投递事务，此刻把它抢走会造成两个 Worker 都认为自己"
        "拥有它，留下双投递窗口。时间比较统一用与存储一致的 naive UTC，避免 aware 值经 sqlite3 适配器"
        "存成带时区后缀的字符串、读回时解析失败。",
        "The candidate query has two groups of conditions with precise boundaries: PENDING with "
        "remind_at <= now (due means processable, equality allowed); CLAIMED with lease_expires_at < now "
        "(strict — a record whose lease expires exactly now is not stolen this round). Why? If equality "
        "were claimable, another worker might be mid-commit on its delivery transaction, and stealing the "
        "record then would leave two workers both believing they own it — a double-delivery window. All "
        "comparisons use naive UTC consistent with storage, so aware values never become "
        "timezone-suffixed strings via the sqlite3 adapter and fail to parse on read-back.",
        [
            ("租约到期 — Lease expiry", "The moment the claim becomes reclaimable"),
            ("边界条件 — Boundary condition", "The exact cutoff of a query"),
            ("双投递窗口 — Double-delivery window", "Two workers both delivering once"),
        ],
        "app/services/reminder_service.py 的候选查询：PENDING 用 <=，CLAIMED 用 <。",
        "The candidate query in app/services/reminder_service.py: <= for PENDING, < for CLAIMED.",
        (
            "“Lease expiry uses strict less-than so an expiring lease is not stolen mid-commit.”",
            "「租约到期用严格小于，避免到期瞬间的记录在提交中途被抢走。」",
        ),
    )

    _knowledge_point(
        doc,
        "CLAIMED 期间的 postpone / complete 策略",
        "Policy for postpone / complete while a reminder is CLAIMED",
        "任务被 Worker 领取（CLAIMED）后，postpone 与 complete 只同步 PENDING 状态的 Reminder：CLAIMED 的"
        "投递按原计划进行（通知照常创建），DELIVERED 的历史永不被改写。语义是「通知反映任务曾到期」——postpone "
        "只影响尚未开始的提醒，不会撤销已在途的投递。这是一次性提醒语义：无 due_at 的 confirm 不创建 Reminder，"
        "postpone 也不凭空创建——历史数据（CONFIRMED 却无 Reminder）保持不变，行为文档化并有测试固定。",
        "Once a task is claimed (CLAIMED), postpone and complete only touch PENDING reminders: a CLAIMED "
        "delivery proceeds as planned (the notification is still created), and DELIVERED history is never "
        "rewritten. The semantics: notifications reflect that the task was once due — postpone affects only "
        "reminders that have not started, and never cancels an in-flight delivery. This is the "
        "one-shot-reminder semantics: confirming without due_at creates no reminder, and postpone does not "
        "invent one — legacy data (CONFIRMED with no reminder) is left untouched, documented and pinned by "
        "tests.",
        [
            ("在途投递 — In-flight delivery", "A delivery already claimed and being processed"),
            ("一次性提醒 — One-shot reminder", "A single due notification per task lifecycle"),
            ("不篡改历史 — Immutable history", "Terminal states are never rewritten"),
        ],
        "app/services/task_service.py 的 postpone_task / complete_task 对 Reminder 的同步带 PENDING 过滤。",
        "postpone_task / complete_task in app/services/task_service.py synchronize reminders with a PENDING filter.",
        (
            "“Postpone only reschedules PENDING reminders; claimed deliveries proceed as planned.”",
            "「postpone 只改期 PENDING 提醒；已领取的投递按原计划执行。」",
        ),
    )

    _knowledge_point(
        doc,
        "列表分页与确定性排序",
        "List pagination and deterministic ordering",
        "列表接口统一分页：limit（默认 50，上限 200）与 offset；负数、零与非法值统一 422。排序是确定性的："
        "reminders 按 remind_at 升序、同值时按 id 升序；notifications 按 created_at 降序、同值时按 id 降序。"
        "固定排序保证分页可复现——相同数据下第 N 页永远相同，页与页不重复不遗漏。unread count 由数据库 COUNT 完成，"
        "不在 Python 里数——列表页与计数永远不会因进程内状态而不一致。",
        "List endpoints share one pagination contract: limit (default 50, cap 200) and offset; negative, "
        "zero, and invalid values return 422 uniformly. Ordering is deterministic: reminders sort by "
        "remind_at ascending with id ascending as tie-breaker; notifications by created_at descending with "
        "id descending. Fixed ordering keeps pagination reproducible — page N is always the same for the "
        "same data, with no duplicates or gaps between pages. The unread count comes from a database COUNT, "
        "never from Python — so the list and the counter cannot drift due to in-process state.",
        [
            ("分页 — Pagination", "limit and offset slicing of a list"),
            ("确定性排序 — Deterministic ordering", "Stable order with a tie-breaker"),
            ("计数 — Counter", "A COUNT query, not an in-memory count"),
        ],
        "app/api/reminders.py 的 _page_params、MAX_PAGE_LIMIT=200 与两套固定排序。",
        "_page_params, MAX_PAGE_LIMIT=200, and the two fixed orderings in app/api/reminders.py.",
        (
            "“Deterministic ordering keeps pagination stable across requests.”",
            "「确定性排序让分页在多次请求之间保持稳定。」",
        ),
    )

    _knowledge_point(
        doc,
        "PostgreSQL 迁移方向",
        "The PostgreSQL migration direction",
        "当前 SQLite 单写者串行化简化了竞争处理——原子领取的条件 UPDATE 天然无冲突。若未来需要多 Worker 并行"
        "与行级锁，迁移 PostgreSQL 是既定方向：Alembic 迁移链已就绪，但当前 SQL 以 SQLite 为准，逐条迁移的"
        "PostgreSQL 分支需要另行验证（例如部分唯一索引在 PostgreSQL 用同一语法族 CREATE UNIQUE INDEX ... WHERE）。"
        "跨库成立的部分：Notification.reminder_id 唯一约束、at-least-once + 去重语义、事务边界设计。诚实声明："
        "迁移是「方向」而非「已完成」，SQLite 下的单写者假设一旦放开，竞争测试需要重写。",
        "SQLite's single-writer serialization simplifies contention handling today — the conditional "
        "UPDATE claim cannot conflict. If multi-worker parallelism and row-level locking ever become a "
        "requirement, PostgreSQL is the planned direction: the Alembic chain is ready, but the current SQL "
        "is SQLite-flavored and each migration needs a verified PostgreSQL branch (for instance, partial "
        "unique indexes use the same syntax family, CREATE UNIQUE INDEX ... WHERE). What holds across "
        "databases: the Notification.reminder_id unique constraint, at-least-once plus dedup semantics, and "
        "the transaction-boundary design. Honest disclaimer: migration is a direction, not a finished "
        "state, and the single-writer assumption, once relaxed, requires rewriting the contention tests.",
        [
            ("行级锁 — Row-level locking", "Per-row locking under multi-writer concurrency"),
            ("迁移分支 — Migration branch", "Per-database dialect paths in the chain"),
            ("多写者 — Multi-writer", "Several writers modifying rows concurrently"),
        ],
        "README「数据库与并发（诚实声明）」与 alembic/versions 迁移链。",
        "The README section 'Database and concurrency (honest declaration)' and the alembic/versions chain.",
        (
            "“The Alembic chain is the migration path if multi-writer concurrency becomes a requirement.”",
            "「若多写者并发成为需求，Alembic 迁移链就是迁移路径。」",
        ),
    )

    # ---------------- 数据库表与约束 ----------------
    doc.add_heading("数据库表与约束", "Database tables and constraints", level=1)
    doc.add_table(
        ("对象 / Object", "关键字段与约束 / Key fields and constraints"),
        [
            [("reminders", "task_id 外键(CASCADE)；status(PENDING/CLAIMED/DELIVERED/CANCELLED/FAILED)；remind_at、claimed_at、lease_expires_at、delivered_at、attempt_count、last_error_code；索引 (status, remind_at)；活动提醒部分唯一索引 uq_reminders_active_task"),
             ("reminders", "task_id FK (CASCADE); status (PENDING/CLAIMED/DELIVERED/CANCELLED/FAILED); remind_at, claimed_at, lease_expires_at, delivered_at, attempt_count, last_error_code; index (status, remind_at); partial unique index uq_reminders_active_task.")],
            [("notifications", "reminder_id 外键且唯一（一个 Reminder 至多一条通知）；task_id 外键；type、title、body、created_at、read_at"),
             ("notifications", "reminder_id FK and unique (at most one per reminder); task_id FK; type, title, body, created_at, read_at.")],
            [("时间约定", "全部 UTC 存储；API 输出统一 Z 后缀；Worker 查询统一 naive UTC 与存储一致"),
             ("Time convention", "All times stored in UTC; API output ends with Z; worker queries use naive UTC to match storage.")],
            [("历史保留", "DELIVERED/CANCELLED/FAILED 记录不删除；任务完成不删通知"),
             ("History retention", "DELIVERED/CANCELLED/FAILED records are never deleted; completing a task keeps its notifications.")],
        ],
        widths=[3.5, 13.0],
    )

    # ---------------- API 教程 ----------------
    doc.add_heading("API 教程", "API tutorial", level=1)
    doc.add_pair(
        "Phase 3 的 API 全部是只读或幂等操作——没有创建 Reminder 的公共接口，提醒只能由 confirm/postpone/complete "
        "的副作用产生。",
        "All Phase 3 APIs are read-only or idempotent — there is no public endpoint that creates reminders; "
        "reminders are produced only by the side effects of confirm, postpone, and complete.",
    )
    doc.add_code("""# 确认有 due_at 的任务后，查看 Reminder
GET /api/reminders?task_id=1
→ [ { "id": 1, "task_id": 1, "status": "PENDING",
      "remind_at": "2026-08-11T04:00:00Z", "attempt_count": 0, ... } ]

# Worker 投递后查看通知
GET /api/notifications?unread_only=true
→ [ { "id": 1, "reminder_id": 1, "task_id": 1, "type": "REMINDER_DUE",
      "title": "任务提醒：报告任务", "body": "任务「报告任务」已到截止时间。",
      "created_at": "2026-08-11T06:00:00Z", "read_at": null } ]

# 未读数与标记已读（幂等）
GET  /api/notifications/unread-count            → { "count": 1 }
POST /api/notifications/1/read                  → { ..., "read_at": "2026-08-11T07:00:00Z" }""")
    doc.add_table(
        ("错误 / Error", "HTTP / Code", "说明 / Meaning"),
        [
            [("Reminder 不存在", "Reminder missing"),
             ("404 REMINDER_NOT_FOUND", "404 REMINDER_NOT_FOUND"),
             ("读取详情或列表时目标不存在", "The target does not exist on read.")],
            [("Notification 不存在", "Notification missing"),
             ("404 NOTIFICATION_NOT_FOUND", "404 NOTIFICATION_NOT_FOUND"),
             ("标记已读时目标不存在", "The target does not exist when marking read.")],
            [("非法 status 过滤值", "Invalid status filter"),
             ("422 VALIDATION_ERROR", "422 VALIDATION_ERROR"),
             ("status 参数不在五种状态内", "The status parameter is not one of the five states.")],
        ],
        widths=[5.0, 5.0, 6.5],
    )

    # ---------------- Worker 运行方式 ----------------
    doc.add_heading("Worker 运行方式", "How to run the worker", level=1)
    doc.add_code("""# 无限轮询（生产/开发）
python -m app.workers.reminder_worker

# 单次处理（CI 与测试）
python -m app.workers.reminder_worker --once

# 固定时钟单次处理（演示与 E2E：无需等待真实时间）
python -m app.workers.reminder_worker --once --now 2026-08-11T06:00:00Z""")
    doc.add_pair(
        "配置：REMINDER_POLL_INTERVAL_SECONDS（轮询间隔）、REMINDER_BATCH_SIZE（每批上限）、"
        "REMINDER_LEASE_SECONDS（租约时长）、REMINDER_MAX_ATTEMPTS（最大尝试次数）。非法值在启动时报清晰配置错误。",
        "Configuration: REMINDER_POLL_INTERVAL_SECONDS (poll interval), REMINDER_BATCH_SIZE (batch cap), "
        "REMINDER_LEASE_SECONDS (lease duration), and REMINDER_MAX_ATTEMPTS (retry ceiling). Invalid "
        "values fail at startup with a clear configuration error.",
    )

    # ---------------- 测试教学 ----------------
    doc.add_heading("测试教学：25 个 Fake Clock 测试怎么组织", "Testing: how the 25 Fake Clock tests are organized", level=1)
    doc.add_pair(
        "测试的骨架与 Phase 2 相同（临时库 + 依赖覆盖 + FixedClock），另加 worker_env 夹具：一个与 API 共享同一"
        "临时库的独立 session，模拟独立 Worker 进程。25 个测试分五组：确认与创建（4）、Worker 投递（7）、"
        "postpone/complete 联动（5）、通知 API（6）、Worker 边界（3）。全部零 sleep——到期靠把 due_at 设在"
        "Fake Clock 之前。",
        "The test skeleton is the same as Phase 2 (temporary database, dependency overrides, FixedClock), "
        "plus a worker_env fixture: a separate session sharing the same temporary database, simulating an "
        "independent worker process. The 25 tests fall into five groups: confirm and creation (4), worker "
        "delivery (7), postpone/complete hooks (5), notification APIs (6), and worker edges (3). All are "
        "sleep-free — expiry is achieved by setting due_at before the Fake Clock.",
    )
    doc.add_code("""# 到期测试的模式：due_at 设在 FIXED_NOW 之前，Worker 时钟在之后
task = make_task(client, due_at="2026-08-11T12:00:00+08:00")  # = 04:00Z
confirm(client, task["id"])
stats = run_once(worker_env, _clock(offset_hours=1),   # 05:00Z，已到期
                 batch_size=50, lease_seconds=60, max_attempts=5)
assert stats.scanned == 1 and stats.delivered == 1""")
    doc.add_pair(
        "SQLite 并发诚实说明：测试用「顺序模拟竞争」（先领一轮、再跑第二轮断言 scanned=0）而不是真实多线程——"
        "SQLite 的写锁行为不适合模拟真正的并行线程竞争，写不稳定测试毫无价值。测试证明的是事务级语义，"
        "不夸大为生产级分布式并发保证。",
        "SQLite concurrency disclosure: tests simulate competition sequentially (claim one round, run a "
        "second round, assert scanned=0) instead of real threads — SQLite's write-lock behavior makes "
        "genuine multi-thread tests flaky and worthless. The tests prove transaction-level semantics and "
        "are not overstated as production-grade distributed concurrency guarantees.",
    )

    # ---------------- 故障注入 ----------------
    doc.add_heading("故障注入：让代码在模拟故障下证明自己", "Fault injection: prove the code under simulated failures", level=2)
    doc.add_pair(
        "测试质量的进阶是故障注入：用 monkeypatch 替换 Session.commit / flush，在第 N 次调用抛异常，模拟磁盘满、"
        "锁冲突、进程崩溃。断言目标不是「代码不崩溃」，而是状态机的确定结果：Reminder 创建 flush 失败 → Task 不残留 "
        "CONFIRMED；领取提交失败 → 保持 PENDING 且 attempt 不变；投递提交失败 → 保持 CLAIMED 等 lease 恢复后恰好一次；"
        "唯一冲突但已存在通知 → 幂等 DELIVERED；唯一冲突且无记录 → FAILED 终态；批次内第一条失败不阻塞第二条。"
        "10 多个故障注入测试分布在 tests/test_phase3_hardening.py 与 tests/test_phase3_review.py，每个故障点"
        "都有对应的可重试 / 终态 / 幂等断言。",
        "The next level of test quality is fault injection: monkeypatch Session.commit / flush to raise on "
        "the Nth call, simulating disk-full, lock conflicts, and process crashes. The assertion target is "
        "not that code survives, but the deterministic state-machine outcome: a failed reminder flush "
        "leaves the task not-CONFIRMED; a failed claim commit keeps it PENDING with attempt unchanged; a "
        "failed delivery commit keeps it CLAIMED for exactly-once after lease recovery; a unique conflict "
        "with an existing notification is idempotent DELIVERED; a unique conflict without one is a "
        "terminal FAILED; and a failed first item never blocks the second. Ten-plus fault-injection tests "
        "live in tests/test_phase3_hardening.py and tests/test_phase3_review.py, each failure point mapped "
        "to a retryable / terminal / idempotent assertion.",
    )
    doc.add_code("""# 故障注入模式：第 2 次 commit（投递提交）抛 OperationalError，模拟数据库锁冲突
from sqlalchemy.orm import Session as SaSession
real_commit = SaSession.commit
calls = {"n": 0}

def flaky_commit(self, *args, **kwargs):
    calls["n"] += 1
    if calls["n"] == 2:                       # 第 1 次 = 领取提交；第 2 次 = 投递提交
        raise OperationalError("UPDATE notifications", {}, RuntimeError("database is locked"))
    return real_commit(self, *args, **kwargs)

monkeypatch.setattr(SaSession, "commit", flaky_commit)
stats = run_once(worker_env, _clock(offset_hours=1), batch_size=50, lease_seconds=60, max_attempts=5)
monkeypatch.undo()
assert stats.claimed == 1 and stats.failed == 0      # SQLAlchemyError 路径：claimed 可重试
assert _reminders(client)[0]["status"] == "CLAIMED"  # 投递未落库、不产生通知""")
    doc.add_pair(
        "一个常见误解：故障注入测试必须断言「没有异常」。恰恰相反——异常被 per-item 隔离捕获是设计，测试断言的是"
        "异常后的数据库状态：要么可重试（PENDING/CLAIMED + attempt 语义正确），要么终态（FAILED/DELIVERED），"
        "绝不出现「半条通知」或「任务确认了但提醒没了」。",
        "A common misconception: fault-injection tests must assert that no exception escapes. The opposite "
        "is true — per-item isolation catching exceptions is the design; tests assert the database state "
        "after the exception: either retryable (PENDING/CLAIMED with correct attempt semantics) or "
        "terminal (FAILED/DELIVERED), never a half-written notification or a confirmed task without its "
        "reminder.",
    )

    # ---------------- 动手实验 ----------------
    doc.add_heading("动手实验", "Hands-on experiments", level=1)

    doc.add_heading("实验一：用 Fake Clock 让 Reminder 到期", "Experiment 1: expire a reminder with the Fake Clock", level=2)
    doc.add_pair(
        "在 pytest 里跑通 test_due_reminder_delivered_with_notification，然后修改 FIXED_NOW 观察断言变化。"
        "再把 due_at 改到 FIXED_NOW 之后，运行 test_pending_not_due_not_delivered——体会「时钟在到期之前，"
        "什么都不发生」。",
        "Run test_due_reminder_delivered_with_notification in pytest, then change FIXED_NOW and watch the "
        "assertions move. Next set due_at after FIXED_NOW and run test_pending_not_due_not_delivered — "
        "experience that before the due time, nothing happens.",
    )

    doc.add_heading("实验二：连续运行两次 Worker 证明不重复通知", "Experiment 2: run the worker twice and prove no duplicate notification", level=2)
    doc.add_pair(
        "跑 test_worker_idempotent_no_duplicate_notification 后，在 shell 里对真实库连续执行两次 "
        "python -m app.workers.reminder_worker --once --now ...，用 GET /api/notifications 数一数——"
        "两次运行只有一条通知。",
        "After running test_worker_idempotent_no_duplicate_notification, execute the worker twice in a "
        "shell against a real database with --once --now, then count notifications via GET "
        "/api/notifications — two runs, one notification.",
    )

    doc.add_heading("实验三：模拟领取后崩溃与 lease 恢复", "Experiment 3: simulate a crash after claiming and lease recovery", level=2)
    doc.add_pair(
        "跑通 test_lease_expired_recovery。修改测试中手工 UPDATE 的 lease_expires_at（未过期 → 过期），"
        "观察 Worker 从「不扫描」变为「恢复投递」，且 attempt_count 从 1 变为 2。",
        "Run test_lease_expired_recovery. Change the lease_expires_at of the manual UPDATE from "
        "not-expired to expired and watch the worker switch from not scanning to recovering delivery, "
        "with attempt_count moving from 1 to 2.",
    )

    doc.add_heading("实验四：postpone Task 并观察 Reminder 变化", "Experiment 4: postpone a task and watch the reminder", level=2)
    doc.add_pair(
        "跑 test_postpone_updates_pending_reminder：确认后 POST postpone 新时间，GET /api/reminders 观察 "
        "remind_at 同步变化且不产生第二个 Reminder。再跑 test_postpone_does_not_touch_delivered_history——"
        "DELIVERED 的提醒时间纹丝不动。",
        "Run test_postpone_updates_pending_reminder: after confirming, POST a new time to postpone and watch "
        "remind_at move without a second reminder. Then run test_postpone_does_not_touch_delivered_history — "
        "a delivered reminder's time stays untouched.",
    )

    doc.add_heading("实验五：complete Task 并观察提醒取消", "Experiment 5: complete a task and watch the reminder cancel", level=2)
    doc.add_pair(
        "跑 test_complete_cancels_pending_reminder：complete 后 Reminder 变为 CANCELLED，通知为空。"
        "再跑 test_complete_keeps_delivered_history：先投递再 complete，通知与 DELIVERED 记录都保留——"
        "历史是审计的一部分，永不删除。",
        "Run test_complete_cancels_pending_reminder: after complete, the reminder becomes CANCELLED and "
        "there are no notifications. Then run test_complete_keeps_delivered_history: deliver first, then "
        "complete — the notification and the DELIVERED record both remain. History is part of the audit "
        "and is never deleted.",
    )

    # ---------------- 技术英语 ----------------
    doc.add_heading("Technical English 技术英语", "Technical English vocabulary", level=1)
    doc.add_pair(
        "以下 22 个核心词汇覆盖 Phase 3 的关键概念。",
        "The 22 core terms below cover the key concepts of Phase 3.",
    )
    vocab = [
        ("Background job", "后台任务", "Work outside the request", "Work executed outside the request path",
         "Reminder delivery runs in the worker, not in the confirm request.",
         "run a background job, schedule a job"),
        ("Worker", "工人/工作进程", "A process that executes work", "A process that executes queued work",
         "app/workers/reminder_worker.py polls and delivers.",
         "run a worker, spawn a worker"),
        ("Scheduler", "调度器", "Decides when work runs", "Decides when work should run",
         "JARVIS uses a polling worker, not a scheduler.",
         "scheduler vs worker, trigger a job"),
        ("Polling", "轮询", "Repeated checks for work", "Repeatedly checking for ready work",
         "The worker queries due PENDING reminders every interval.",
         "poll the database, poll interval"),
        ("Source of truth", "事实来源", "Where the real state lives", "The place where the real state lives",
         "The reminders table holds all reminder state.",
         "single source of truth"),
        ("State machine", "状态机", "Fixed states and transitions", "Fixed states and transitions",
         "PENDING to CLAIMED to DELIVERED, or CANCELLED, or FAILED.",
         "transition between states, terminal state"),
        ("Claim", "领取", "Taking ownership atomically", "Atomically taking ownership of a record",
         "A conditional UPDATE claims the reminder.",
         "claim a record, atomic claim"),
        ("Lease", "租约", "A claim with a deadline", "A temporary exclusive claim with a deadline",
         "lease_expires_at lets crashed workers be recovered.",
         "acquire a lease, lease expiry"),
        ("Conditional update", "条件更新", "Update guarded by a condition", "An update that only applies when the condition holds",
         "The claim UPDATE checks status and due time in the WHERE clause.",
         "conditional update, race-free update"),
        ("Idempotent", "幂等", "Repeating equals doing once", "Repeating has the same effect as doing once",
         "Re-running the worker never duplicates a notification.",
         "idempotent delivery, idempotent API"),
        ("Exactly-once", "恰好一次", "Exactly one delivery", "Exactly one delivery, never more",
         "Not claimed here; JARVIS implements at-least-once plus dedup.",
         "exactly-once semantics, hard to achieve"),
        ("At-least-once", "至少一次", "At least one delivery", "At least one delivery, possibly more",
         "The worker may retry; dedup makes it look like once.",
         "at-least-once delivery"),
        ("Unique constraint", "唯一约束", "The database rejects duplicates", "A rule the database enforces on duplicates",
         "Notification.reminder_id is unique.",
         "enforce uniqueness, final guard"),
        ("Transaction", "事务", "Writes that commit together", "Writes that commit or roll back together",
         "Notification and DELIVERED commit in one transaction.",
         "commit atomically, roll back"),
        ("Crash recovery", "崩溃恢复", "Recovering after a crash", "Recovering work after a process dies",
         "An expired lease lets another worker reclaim.",
         "recover after a crash"),
        ("Attempt count", "尝试次数", "How many tries so far", "How many times delivery was tried",
         "attempt_count increments at every claim.",
         "count attempts, bounded retries"),
        ("Poison message", "毒消息", "A record that keeps failing", "A record that keeps failing and must be quarantined",
         "MAX_ATTEMPTS turns it into FAILED.",
         "poison message, quarantine"),
        ("Graceful shutdown", "优雅退出", "Finishing work before exit", "Finishing current work before exiting",
         "SIGTERM sets a flag; the loop exits after the current round.",
         "shut down gracefully"),
        ("Fake Clock", "假时钟", "A test double freezing time", "A test double that freezes time",
         "FixedClock drives every reminder test.",
         "freeze time, inject a clock"),
        ("UTC", "协调世界时", "The global time standard", "The global time standard",
         "All reminder timestamps are stored in UTC.",
         "store in UTC, naive UTC"),
        ("Side effect", "副作用", "A change caused by an action", "An external change caused by an action",
         "Confirming a task creates a reminder in the same transaction.",
         "attach a side effect, side-effect-free"),
        ("Notification center", "通知中心", "The UI for notifications", "The UI where users see notifications",
         "The frontend bell with unread count and mark-read.",
         "unread count, mark as read"),
    ]
    doc.add_table(
        ("术语 / Term", "含义 / Meaning", "定义 / Simple definition", "JARVIS 实例 / Example", "常见搭配 / Phrases"),
        [[cn, en_meaning, definition, example, phrases] for term, cn, en_meaning, definition, example, phrases in vocab],
        widths=[2.9, 2.0, 4.8, 3.4, 2.9],
    )

    # ---------------- 代码口头表达 ----------------
    doc.add_heading("Reading Code Aloud 代码口头表达", "How to describe code in English", level=1)
    reading = [
        (
            "确认事务（task_service.py）",
            "“Confirming a task changes its status and creates a pending reminder in the same transaction, so the two can never diverge.”",
            "「确认任务时状态变更与提醒创建在同一事务，两者永远不会不一致。」",
            "无 due_at 的任务不创建提醒；重复确认被状态机拒绝。",
            "Tasks without a due date get no reminder; duplicate confirms are rejected by the state machine.",
        ),
        (
            "轮询查询（reminder_service.py）",
            "“The worker polls for reminders that are pending and due, or claimed with an expired lease, and processes them in batches.”",
            "「Worker 轮询 PENDING 且到期、或 CLAIMED 且租约过期的提醒，并按批次处理。」",
            "时钟来自注入的 Clock，测试用 FixedClock 冻结时间。",
            "The clock comes from the injected Clock; tests freeze time with FixedClock.",
        ),
        (
            "原子领取（reminder_service.py）",
            "“The claim is a conditional update, so exactly one worker wins the race and the loser is skipped.”",
            "「领取是一条条件更新，所以恰好一个 Worker 赢得竞争，输家被跳过。」",
            "领取即计数：attempt_count 在领取时加一。",
            "Claiming counts: attempt_count increments at claim time.",
        ),
        (
            "幂等投递（reminder_service.py）",
            "“Delivery is idempotent: the unique constraint deduplicates, so rerunning the worker never creates a second notification.”",
            "「投递是幂等的：唯一约束去重，重复运行 Worker 不会产生第二条通知。」",
            "唯一冲突被当作「已投递」处理，而不是报错。",
            "A unique conflict is treated as already delivered instead of an error.",
        ),
        (
            "优雅退出（reminder_worker.py）",
            "“On a stop signal the worker finishes the current round and exits cleanly, so in-flight deliveries are never truncated.”",
            "「收到停止信号后 Worker 完成当前一轮再退出，进行中的投递不会被截断。」",
            "单轮异常只记录不退出，毒消息打不死 Worker。",
            "Per-round exceptions are logged, not fatal — a poison message never kills the worker.",
        ),
    ]
    for title, sentence, meaning, note_cn, note_en in reading:
        doc.add_heading(title, title, level=3)
        doc.add_pair(sentence, meaning)
        doc.add_label("延伸说明 / Extended note")
        doc.add_pair(note_cn, note_en)

    # ---------------- 面试英语 ----------------
    doc.add_heading("Interview English 面试英语", "Interview practice", level=1)
    interviews = [
        (
            "Worker 和 Scheduler 有什么区别？",
            "What is the difference between a worker and a scheduler?",
            "Scheduler 决定「什么时候运行」，Worker 负责「执行」。JARVIS 用轮询型 Worker：没有闹钟，每隔几秒查一次"
            "数据库里有没有到期的工作。",
            "A scheduler decides when to run; a worker does the work. JARVIS uses a polling worker: no alarm, "
            "it checks the database every few seconds for due work.",
            "polling worker, due work",
        ),
        (
            "为什么不用内存计时器？",
            "Why no in-memory timers?",
            "内存状态在进程崩溃时全部丢失。数据库是唯一事实来源：提醒状态、租约、尝试次数都在库里，"
            "任何进程重启都不丢状态。",
            "Memory dies with the process. The database is the single source of truth: reminder status, "
            "leases, and attempt counts live there, so restarts lose nothing.",
            "single source of truth, survive restarts",
        ),
        (
            "两个 Worker 竞争同一条提醒会发生什么？",
            "What happens when two workers compete for the same reminder?",
            "原子领取用条件 UPDATE：条件在 SQL 里，数据库保证只有一个 UPDATE 影响该行，另一个得到 rowcount=0 "
            "被跳过。通知的唯一约束是最终防线。",
            "Atomic claiming is a conditional update: the conditions are in the SQL, the database guarantees "
            "only one update affects the row, and the other gets rowcount 0 and is skipped. The unique "
            "constraint on notifications is the final guard.",
            "atomic claim, rowcount, final guard",
        ),
        (
            "lease 解决了什么问题？",
            "What problem does the lease solve?",
            "Worker 领取后可能崩溃。lease 是一个截止时间：租约未到期其他 Worker 不能碰这条记录；到期后说明"
            "原 Worker 大概率死了，可以重新领取恢复。",
            "A worker may crash after claiming. The lease is a deadline: while it is unexpired, no other "
            "worker touches the record; after expiry the original worker is presumed dead and the record "
            "can be reclaimed.",
            "presumed dead, reclaim",
        ),
        (
            "如何保证不重复通知？",
            "How do you prevent duplicate notifications?",
            "三层：领取是条件更新（只有一个 Worker 获胜）；DELIVERED 状态让已投递记录不再被扫描；"
            "Notification.reminder_id 唯一约束是数据库层面的最终防线。",
            "Three layers: the claim is a conditional update (only one winner); DELIVERED removes the record "
            "from future scans; and the unique constraint on reminder_id is the database-level final guard.",
            "three layers, final guard",
        ),
        (
            "exactly-once 为什么难以实现？",
            "Why is exactly-once hard to achieve?",
            "因为「写入」和「确认写入成功」之间可能崩溃：通知已写入但提交前 Worker 死了，重试就会重复。"
            "我们诚实实现 at-least-once 加去重，而不是夸大承诺。",
            "Because a crash can happen between writing and confirming the write: the notification is "
            "inserted but the worker dies before committing, so a retry duplicates it. We honestly "
            "implement at-least-once plus dedup rather than overclaiming.",
            "crash window, overclaim",
        ),
        (
            "重试为什么要有上限？",
            "Why must retries be bounded?",
            "无限重试会让毒消息永远占用 Worker 和数据库。达到 MAX_ATTEMPTS 后提醒变为 FAILED 并记录错误码，"
            "问题暴露给运维而不是永远重试。",
            "Unbounded retries let a poison message occupy the worker and database forever. At "
            "MAX_ATTEMPTS the reminder becomes FAILED with an error code, surfacing the problem to "
            "operations instead of retrying endlessly.",
            "poison message, bounded retries",
        ),
        (
            "postpone 任务时 Reminder 如何处理？",
            "How are reminders handled when a task is postponed?",
            "同一事务更新 PENDING 提醒的 remind_at 为新 due_at。CLAIMED（正在投递）和 DELIVERED 历史不修改——"
            "正在投递的提醒已不可撤回，历史是审计。",
            "The same transaction updates the PENDING reminder's remind_at to the new due time. CLAIMED "
            "(in-flight) and DELIVERED history stay untouched — an in-flight delivery cannot be recalled, "
            "and history is the audit.",
            "in-flight delivery, audit history",
        ),
        (
            "测试时间敏感逻辑为什么用 Fake Clock？",
            "Why use a Fake Clock for time-sensitive tests?",
            "等待真实时间既慢又不可靠。Fake Clock 把「现在」变成可注入依赖，测试把时钟设到任意时刻，"
            "到期测试瞬时完成且完全确定。",
            "Waiting for real time is slow and flaky. The Fake Clock turns \"now\" into an injectable "
            "dependency; tests set the clock to any instant, so expiry tests run instantly and "
            "deterministically.",
            "injectable clock, deterministic tests",
        ),
        (
            "为什么 Worker 不放进 FastAPI 生命周期？",
            "Why is the worker not inside FastAPI's lifecycle?",
            "开发时 uvicorn --reload 会启动多个进程实例，如果 Worker 循环在生命周期里就会有多个 Worker 同时轮询。"
            "独立进程让实例数量可控。",
            "In development, uvicorn --reload spawns multiple process instances; if the worker loop lived in "
            "the lifecycle, multiple workers would poll the same records. A separate process keeps the "
            "instance count under control.",
            "hot reload, instance count",
        ),
    ]
    for i, (q_cn, q_en, a_cn, a_en, expr) in enumerate(interviews, start=1):
        doc.add_heading(f"面试题 {i}", f"Interview question {i}", level=3)
        doc.add_label("问题")
        doc.add_cn_only(q_cn)
        doc.add_label("Question")
        doc.add_en_only(q_en)
        doc.add_label("参考回答")
        doc.add_cn_only(a_cn)
        doc.add_label("Answer")
        doc.add_en_only(a_en)
        doc.add_pair(f"重要表达：{expr}", f"Key phrases: {expr}")

    # ---------------- 一分钟介绍 ----------------
    doc.add_heading("Sixty-second Introduction 一分钟项目介绍", "Introducing Phase 3 in one minute", level=1)
    intro_cn = (
        "Phase 3 我给 JARVIS 实现了数据库驱动的提醒调度。确认有截止时间的任务时，同一事务创建待提醒记录；"
        "独立 Worker 进程轮询数据库，用条件更新原子领取到期提醒，设置租约防止重复处理，投递应用内通知后标记已投递。"
        "幂等靠三层保证：条件领取、已投递状态、通知唯一约束——重复运行 Worker 不会产生第二条通知。崩溃恢复靠租约："
        "Worker 死了，租约到期后其他 Worker 重新领取。重试有上限，达到最大次数后标记失败。前端有通知中心，"
        "未读计数、列表、标记已读都是幂等的。25 个新测试全部用 Fake Clock，零 sleep，加上原有测试共 122 个全部通过。"
    )
    intro_en = (
        "In Phase 3 I implemented database-driven reminder scheduling for JARVIS. Confirming a task with a "
        "due date creates a pending reminder in the same transaction; a standalone worker polls the "
        "database, claims due reminders atomically with a conditional update, sets a lease to prevent "
        "double processing, delivers an in-app notification, and marks the reminder delivered. "
        "Idempotency has three layers: conditional claiming, the delivered state, and a unique constraint "
        "on notifications — rerunning the worker never creates a second notification. Crash recovery "
        "relies on the lease: when a worker dies, another reclaims after expiry. Retries are bounded and "
        "fail permanently at the maximum. The frontend has a notification center with idempotent unread "
        "counts, lists, and mark-read. All 25 new tests use a Fake Clock with zero sleep, and the full "
        "suite of 122 passes."
    )
    doc.add_label("中文版本")
    doc.add_cn_only(intro_cn)
    doc.add_label("English version")
    doc.add_en_only(intro_en)
    doc.add_pair(
        "断句提示：六句——① 提醒闭环；② 原子领取与租约；③ 三层幂等；④ 崩溃恢复与重试上限；⑤ 前端通知中心；"
        "⑥ 测试证据。强调词：same transaction、conditional update、lease、three layers、bounded retries、"
        "Fake Clock、122 passes。",
        "Pacing: six sentences — ① the reminder loop; ② atomic claiming and leases; ③ three-layer "
        "idempotency; ④ crash recovery and bounded retries; ⑤ the frontend notification center; ⑥ the "
        "testing evidence. Stress: same transaction, conditional update, lease, three layers, bounded "
        "retries, Fake Clock, 122 passes.",
    )

    # ---------------- 写作练习 ----------------
    doc.add_heading("Writing Practice 写作练习", "Writing practice", level=1)
    writing = [
        "Explain in English how a conditional update makes reminder claiming race-free.",
        "Explain in English the three layers that prevent duplicate notifications.",
        "Write an API error report in English: marking a notification read returns 404 NOTIFICATION_NOT_FOUND. Describe the request, the response, and the likely cause.",
        "Explain in English why postpone only updates PENDING reminders and leaves DELIVERED history untouched.",
        "Explain in English how the lease enables crash recovery and why attempt counts are bounded.",
    ]
    for i, q in enumerate(writing, start=1):
        doc.add_heading(f"写作题 {i}", f"Writing task {i}", level=3)
        doc.add_en_only(q)

    doc.add_heading("写作练习参考答案", "Writing practice reference answers", level=1)
    answers = [
        (
            "A conditional update puts the claim conditions in the UPDATE's WHERE clause: the status must "
            "be PENDING with a due time in the past, or CLAIMED with an expired lease. When two workers "
            "execute the same update, the database serializes them and only one affects the row; the "
            "other sees rowcount zero and skips. There is no check-then-act window, so the claim is "
            "race-free without locks.",
            "条件更新把领取条件放在 UPDATE 的 WHERE 子句里：状态必须是 PENDING 且到期，或 CLAIMED 且租约过期。"
            "两个 Worker 执行同一条更新时，数据库串行化它们，只有一个影响该行；另一个看到 rowcount 为零并跳过。"
            "没有先查后改的窗口，所以无需锁就能无竞态领取。",
        ),
        (
            "Layer one: the claim is a conditional update, so only one worker wins the race. Layer two: "
            "the DELIVERED state removes the reminder from future scans. Layer three: the unique "
            "constraint on notifications.reminder_id makes a duplicate insert conflict at the database "
            "level, and the conflict is treated as already delivered. Even a buggy retry cannot create a "
            "second notification.",
            "第一层：领取是条件更新，只有一个 Worker 获胜。第二层：DELIVERED 状态让提醒退出后续扫描。第三层："
            "notifications.reminder_id 的唯一约束让重复插入在数据库层面冲突，冲突被当作已投递处理。"
            "即使重试逻辑有 bug 也无法产生第二条通知。",
        ),
        (
            "Request: POST /api/notifications/42/read. Response: HTTP 404 with "
            "{\"error\": {\"code\": \"NOTIFICATION_NOT_FOUND\", \"message\": \"Notification 42 does not "
            "exist\"}}. The likely cause is that the notification was never created — for example, the "
            "reminder has not been delivered yet — or the client used a stale id. The fix is to refresh "
            "the notification list before acting.",
            "请求：POST /api/notifications/42/read。响应：HTTP 404 加统一错误结构。可能的原因是通知从未创建——"
            "例如提醒尚未投递——或者客户端使用了过期的 id。修复方法是先刷新通知列表再操作。",
        ),
        (
            "Postpone only updates PENDING reminders because that is the only state still waiting for the "
            "new time. A CLAIMED reminder is being delivered right now — the old time already passed — so "
            "rewriting it would be meaningless and would race with the worker. DELIVERED reminders are "
            "history: part of the audit, never modified. The rule is simple: only the waiting state "
            "follows the new due time.",
            "postpone 只更新 PENDING 提醒，因为那是唯一还在等待新时间的状态。CLAIMED 提醒正在投递——旧时间已经"
            "过去——改写它既无意义又会与 Worker 竞争。DELIVERED 是历史：审计的一部分，永不修改。规则很简单："
            "只有等待中的状态跟随新截止时间。",
        ),
        (
            "After claiming, the worker sets a lease: a deadline in lease_expires_at. If the worker "
            "crashes before delivery, the record stays CLAIMED until the lease expires, and then another "
            "worker may reclaim it — that is crash recovery. Attempt counts are bounded because a "
            "permanently failing record must not retry forever: at MAX_ATTEMPTS it becomes FAILED with "
            "an error code, so operations can investigate instead of watching infinite retries.",
            "领取后 Worker 设置租约：lease_expires_at 里的截止时间。如果 Worker 在投递前崩溃，记录保持 CLAIMED "
            "直到租约过期，之后其他 Worker 可以重新领取——这就是崩溃恢复。尝试次数有上限，因为永久失败的记录"
            "不能永远重试：达到 MAX_ATTEMPTS 后变成 FAILED 并带错误码，运维可以调查而不是看着无限重试。",
        ),
    ]
    for i, (en, cn) in enumerate(answers, start=1):
        doc.add_heading(f"参考答案 {i}", f"Reference answer {i}", level=3)
        doc.add_en_only(en)
        doc.add_cn_only(cn)

    # ---------------- 口语练习 ----------------
    doc.add_heading("Speaking Practice 口语练习", "Speaking practice", level=1)
    speaking = [
        ("一句话版：用一句英文说出 Worker 的职责。",
         "One-sentence version: state the worker's job in one English sentence.",
         "The worker polls the database for due reminders, claims them atomically, and delivers one notification each.",
         "Worker 轮询数据库里到期的提醒，原子领取它们，并各投递一条通知。"),
        ("一句话版：用一句英文解释幂等投递。",
         "One-sentence version: explain idempotent delivery in one sentence.",
         "Delivery is idempotent because a unique constraint makes a duplicate attempt harmless.",
         "投递是幂等的，因为唯一约束让重复尝试变得无害。"),
        ("30 秒版：描述两个 Worker 竞争同一条提醒的过程。",
         "30-second version: describe two workers competing for the same reminder.",
         "Both see the same due reminder. Both run the same conditional update, but the database only lets one affect the row. The loser sees rowcount zero and skips, and the unique constraint on notifications is the final guarantee.",
         "两个 Worker 看到同一条到期提醒。它们执行同一条条件更新，但数据库只允许一个影响该行。输家看到 rowcount "
         "为零并跳过，通知上的唯一约束是最终保障。"),
        ("30 秒版：解释租约如何支持崩溃恢复。",
         "30-second version: explain how the lease supports crash recovery.",
         "After claiming, the worker sets a lease deadline. If it crashes, the record stays claimed until the lease expires, then another worker reclaims it and keeps trying, with the attempt count growing.",
         "领取后 Worker 设置租约截止时间。如果它崩溃，记录保持已领取直到租约过期，然后其他 Worker 重新领取并继续，"
         "尝试次数持续累加。"),
        ("一分钟版：向同事解释为什么确认任务时要同时创建提醒。",
         "One-minute version: explain to a colleague why confirming a task creates the reminder in the same transaction.",
         "Confirming a task changes its status and creates the reminder in one database transaction. This "
         "guarantees consistency: a confirmed task with a due date always has a reminder, and a reminder "
         "never exists for an unconfirmed draft. If the transaction fails, both changes roll back together. "
         "It also keeps creation side-effect-free: proposals only create drafts, and no reminder appears "
         "until the user actually confirms. The same pattern applies to postpone, which moves the pending "
         "reminder's time, and complete, which cancels it.",
         "确认任务时状态变更与提醒创建在同一个数据库事务里。这保证一致性：有截止时间的已确认任务一定有提醒，"
         "未确认的草稿永远不会出现提醒。事务失败时两者一起回滚。它也保持创建无副作用：建议只创建草稿，"
         "用户真正确认前不会有提醒。同样的模式用于 postpone（移动待提醒的时间）和 complete（取消提醒）。"),
    ]
    for i, (cn, en, answer, meaning) in enumerate(speaking, start=1):
        doc.add_heading(f"口语练习 {i}", f"Speaking practice {i}", level=3)
        doc.add_pair(cn, en)
        doc.add_label("示范回答")
        doc.add_en_only(answer)
        doc.add_cn_only(meaning)

    # ---------------- 常见错误排查 ----------------
    doc.add_heading("常见错误排查", "Troubleshooting common mistakes", level=1)
    doc.add_table(
        ("症状 / Symptom", "原因 / Cause", "修复 / Fix"),
        [
            [("Worker 扫描数为 0 但提醒明明到期", "Worker scans 0 but the reminder is due"),
             ("Fake Clock 的时刻早于 remind_at；或测试数据用了未来的 due_at", "The Fake Clock is before remind_at; or the test used a future due_at"),
             ("把 due_at 设在时钟之前；用 --now 固定时钟再验证", "Set due_at before the clock; verify with --now.")],
            [("TypeError: can't compare offset-naive and offset-aware", "TypeError: can't compare offset-naive and offset-aware"),
             ("aware 参数与 SQLite 读回的 naive 值混比", "Aware parameters compared with naive values read back from SQLite"),
             ("统一 _utc_naive()：所有写入与查询参数转 naive UTC", "Normalize every parameter with _utc_naive() to naive UTC.")],
            [("重复运行 Worker 出现唯一约束异常日志", "Unique constraint errors on rerun"),
             ("唯一冲突被当作异常而不是幂等路径", "The unique conflict is treated as an error instead of the idempotent path"),
             ("检查 _deliver 的 IntegrityError 分支：先查已存在通知再标记 DELIVERED", "Check _deliver's IntegrityError branch: confirm the notification exists, then mark DELIVERED.")],
            [("upgrade head 报表已存在", "upgrade head says table exists"),
             ("测试的 create_all 在开发库建了表而版本号未推进", "Tests' create_all built the table but the version did not advance"),
             ("结构一致时 alembic stamp <revision> 对齐版本", "When the schema matches, stamp the revision.")],
            [("Worker 投递后通知数量翻倍", "Notifications double after delivery"),
             ("丢失了 DELIVERED 状态更新或唯一约束被移除", "The DELIVERED update or the unique constraint is missing"),
             ("确认投递事务同时写 Notification 与 DELIVERED；检查唯一索引存在", "Confirm the delivery transaction writes both; check the unique index exists.")],
        ],
        widths=[4.5, 6.0, 6.0],
    )

    # ---------------- 下一阶段预告 ----------------
    # ---------------- 安全边界 ----------------
    doc.add_heading("安全边界：单用户应用的限制", "Security boundary: single-user limits", level=1)
    doc.add_pair(
        "JARVIS 没有认证与用户隔离：任何能访问服务的人都能看到任务、对话与通知。README 在开头醒目声明"
        "「仅适合单用户本地开发与学习，不得直接部署公网」。我们不做「看起来像多用户」的界面——没有登录、"
        "没有 per-user 数据隔离承诺。通知中心是应用内通知，未实现邮件/短信/微信等外部渠道。",
        "JARVIS has no authentication or user isolation: anyone who can reach the service can see tasks, "
        "conversations, and notifications. The README opens with a prominent declaration that it is for "
        "single-user local development and learning only and must not be deployed to the public internet. "
        "We do not build a UI that looks multi-user — there is no login and no per-user isolation promise. "
        "The notification center is in-app only; email, SMS, and WeChat channels are not implemented.",
    )
    doc.add_pair(
        "攻击面收窄：Worker 只执行数据库内已定义的动作——创建 Notification、更新 Reminder 状态，绝不执行 "
        "LLM 输出的正文。Prompt Injection 的防线在 Phase 2 的白名单（仅 create_task_draft），Phase 3 没有引入"
        "任何新的 LLM 执行面，注入无法让 Worker 投递任意内容以外的动作。",
        "The attack surface is narrow: the worker only performs actions defined in the database — creating "
        "a notification and updating reminder status — and never executes the body of LLM output. The "
        "prompt-injection defense is Phase 2's whitelist (create_task_draft only); Phase 3 adds no new LLM "
        "execution surface, so injection cannot make the worker do anything beyond delivering content.",
    )
    doc.add_pair(
        "日志脱敏：错误日志只记录异常类型与脱敏消息（sanitize_error_message 移除数据库 URL 与 API Key 并截断"
        "超长内容），本地日志不会意外泄露密钥；API 错误响应也不暴露数据库内部错误细节。",
        "Log redaction: error logs record only the exception type and a sanitized message "
        "(sanitize_error_message strips the database URL and API key and truncates long content), so local "
        "logs cannot leak secrets; API error responses likewise never expose internal database error "
        "details.",
    )

    doc.add_heading("下一阶段预告", "Preview of the next phase", level=1)
    doc.add_pair(
        "Phase 3 让 JARVIS 记住时间。下一步的自然方向：一是通知渠道扩展（邮件/Webhook），但必须沿用本阶段的"
        "幂等纪律——每个渠道的投递记录同样受唯一约束保护；二是把 Reminder 与 Phase 2 的 Proposal 建议闭环结合"
        "（建议生成时展示「确认后将于 X 提醒」的预览）；三是引入更正式的任务队列（如任务表 + 队列抽象）以支持"
        "更复杂的调度，但 SQLite 下的单写者模型必须被诚实评估。",
        "Phase 3 gives JARVIS a sense of time. Natural next steps: first, notification channel expansion "
        "(email, webhooks) — but only with this phase's idempotency discipline, where every channel's "
        "delivery record is protected by its own unique constraint; second, combining reminders with "
        "Phase 2's proposal loop (the preview says \"a reminder will be scheduled at X after confirm\"); "
        "third, a more formal job queue, but the single-writer model of SQLite must be honestly evaluated.",
    )
    doc.add_pair(
        "完成 Phase 3 的学习后，请回到真实代码：通读 app/services/reminder_service.py、app/workers/"
        "reminder_worker.py 与 tests/test_reminders.py，然后用英文向自己复述一遍从确认到通知的完整闭环。",
        "After finishing Phase 3, return to the real code: read app/services/reminder_service.py, "
        "app/workers/reminder_worker.py, and tests/test_reminders.py, then retell the full loop from "
        "confirmation to notification in English.",
    )


