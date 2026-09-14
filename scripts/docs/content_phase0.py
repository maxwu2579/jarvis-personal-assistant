"""Phase 0 学习文档内容：后端基础与任务状态机。

内容组织遵循项目文档规范：
- 每个知识点固定五段式：中文解释 / English Explanation / 关键词 / 项目实例 / 英语表达练习；
- 代码讲解：代码作用 / Purpose / 逐步执行 / 为什么这样设计 / 常见错误；
- 中英段落严格逐段配对。
"""

META = {
    "title_cn": "阶段 0：后端基础与任务状态机",
    "title_en": "Phase 0: Backend Foundations and the Task State Machine",
    "subtitle_cn": "JARVIS 项目双语学习文档 · 中文解释与英文表达逐段对照",
    "subtitle_en": "Bilingual learning material for the JARVIS project",
    "header": "JARVIS 双语学习 · 阶段 0 / Phase 0",
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
    """固定教学结构：中文解释 → English Explanation → 关键词 → 项目实例 → 英语表达练习。"""
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
    # ---------------- 文档说明 ----------------
    doc.add_heading("文档说明", "About this document", level=1)
    doc.add_pair(
        "这是一份中英双语学习文档，面向 JARVIS 项目的学习者。每个知识点都先用中文用零基础、生活化的语言解释，"
        "再用简单的技术英语复述一遍。我们的目标有三个：学习 JARVIS 的后端与大模型应用技术、学习相关技术英语、"
        "训练用中英文解释项目架构和技术决策。",
        "This is a bilingual learning document for the JARVIS project. Every topic is first explained in Chinese "
        "with plain, beginner-friendly language, then repeated in simple technical English. We have three goals: "
        "learn JARVIS backend and LLM application techniques, learn technical English, and practice explaining "
        "the architecture and technical decisions in both languages.",
    )
    doc.add_pair(
        "英文段落紧跟对应的中文段落，颜色为深蓝色并带有缩进，方便对照阅读。代码块只保留一份，不重复粘贴中英文版本。"
        "所有代码都来自 JARVIS 仓库中的真实文件。",
        "Each English paragraph directly follows its Chinese partner and is shown in dark blue with an indent. "
        "Code is shown only once, never duplicated in two languages. All code examples come from real files "
        "in the JARVIS repository.",
    )

    # ---------------- 项目总览 ----------------
    doc.add_heading("项目总览与架构", "Project overview and architecture", level=1)
    doc.add_pair(
        "JARVIS 是一个个人 AI 助理项目。Phase 0 是第一个阶段，完成了一个可以运行的「任务管理」垂直切片："
        "后端用 Python + FastAPI 提供 HTTP 接口，数据库用 SQLite，前端是 React 单页应用。"
        "任务有严格的三种状态（草稿、已确认、已完成），状态之间只能按规则转换，非法操作会被后端拒绝。",
        "JARVIS is a personal AI assistant project. Phase 0 is the first stage, delivering a working "
        "'task management' vertical slice: the backend provides HTTP APIs with Python + FastAPI, the database "
        "is SQLite, and the frontend is a React single-page application. Tasks have exactly three states "
        "(draft, confirmed, done), transitions follow strict rules, and invalid operations are rejected "
        "by the backend.",
    )
    doc.add_architecture_table(
        "JARVIS 后端分层架构（Phase 0）",
        "JARVIS backend layered architecture (Phase 0)",
        [
            (
                "API 层（api/）",
                "API layer (api/)",
                "接收 HTTP 请求，解析参数，把业务异常映射为稳定的 HTTP 状态码（404/409/422）。",
                "Receives HTTP requests, parses parameters, and maps business errors to stable HTTP status codes (404/409/422).",
            ),
            (
                "服务层（services/）",
                "Service layer (services/)",
                "承载全部业务规则：任务状态机、非法转换检测。不感知 HTTP。",
                "Holds all business rules: the task state machine and invalid-transition checks. It knows nothing about HTTP.",
            ),
            (
                "模型层（models/）",
                "Model layer (models/)",
                "SQLAlchemy 模型定义表结构，负责持久化；所有时间统一 UTC。",
                "SQLAlchemy models define table structures and handle persistence; all timestamps are stored in UTC.",
            ),
            (
                "核心层（core/）",
                "Core layer (core/)",
                "应用配置（环境变量）与数据库引擎/会话管理。",
                "Application configuration (environment variables) and database engine/session management.",
            ),
        ],
    )
    doc.add_pair(
        "分层的好处是职责清晰：HTTP 的问题只在 API 层处理，业务规则集中在服务层，测试可以直击业务逻辑而不被网络干扰。",
        "The benefit of layering is clear responsibility: HTTP concerns stay in the API layer, business rules "
        "live in the service layer, and tests can target business logic without network interference.",
    )

    # ---------------- 知识点 ----------------
    doc.add_heading("核心知识点", "Core knowledge points", level=1)

    _knowledge_point(
        doc,
        "FastAPI：把 Python 函数变成 HTTP 接口",
        "FastAPI: turning Python functions into HTTP endpoints",
        "FastAPI 是一个 Python Web 框架，它做一件核心的事：你写一个普通的 Python 函数，它帮你把这个函数变成一个"
        "可以通过网址调用的接口。比如浏览器或前端程序向服务器发送一个请求，FastAPI 负责把请求送到你的函数里，"
        "再把函数的返回值变成 JSON 发回去。它还能自动生成交互式接口文档（/docs），方便调试。",
        "FastAPI is a Python web framework that does one core thing: you write an ordinary Python function, and "
        "it turns that function into an endpoint callable over the network. When a browser or frontend sends a "
        "request, FastAPI delivers it to your function and converts the return value back into JSON. It also "
        "auto-generates interactive API documentation at /docs.",
        [
            ("接口/端点 — Endpoint", "A URL that maps to a server function"),
            ("请求 — Request", "What the client sends to the server"),
            ("响应 — Response", "What the server sends back"),
            ("JSON — JSON", "A text format for structured data exchange"),
            ("自动文档 — Auto docs", "Interactive documentation generated by FastAPI"),
        ],
        "在 backend/app/api/tasks.py 中，@router.post(\"/api/tasks\") 装饰的函数就是 FastAPI 路由：前端 POST 一个"
        "JSON 请求体，函数创建任务并返回 201 响应。",
        "In backend/app/api/tasks.py, the function decorated with @router.post(\"/api/tasks\") is a FastAPI route: "
        "the frontend POSTs a JSON body, and the function creates a task and returns a 201 response.",
        (
            "“I built a REST API with FastAPI. Each endpoint validates the request and delegates the business "
            "logic to the service layer.”",
            "「我用 FastAPI 构建了 REST API。每个接口都先校验请求，再把业务逻辑交给服务层。」",
        ),
    )

    _knowledge_point(
        doc,
        "HTTP 与 API：前端和后端如何对话",
        "HTTP and APIs: how the frontend talks to the backend",
        "API（应用程序编程接口，Application Programming Interface）是程序之间对话的约定。JARVIS 使用 HTTP 协议："
        "前端发一个「请求」到网址，后端返回一个「响应」。HTTP 动词表达意图——POST 表示创建，GET 表示读取，PATCH 表示修改。"
        "状态码表达结果——200 表示成功，201 表示创建成功，404 表示没找到，409 表示冲突，422 表示参数不合法。",
        "An API is a contract for programs to talk to each other. JARVIS uses the HTTP protocol: the frontend "
        "sends a request to a URL, and the backend returns a response. HTTP verbs express intent — POST means "
        "create, GET means read, PATCH means update. Status codes express the result — 200 means OK, 201 means "
        "created, 404 means not found, 409 means conflict, and 422 means invalid input.",
        [
            ("动词 — Verb", "The intent of a request: GET, POST, PATCH, ..."),
            ("状态码 — Status code", "A three-digit number describing the result"),
            ("路由 — Route", "The URL path that identifies an endpoint"),
            ("请求体 — Request body", "Data sent with a POST or PATCH request"),
        ],
        "backend/app/main.py 把服务层异常（TaskNotFoundError）映射为 404，把 InvalidTransitionError 映射为 409，"
        "客户端读到统一结构的错误 JSON。",
        "backend/app/main.py maps service exceptions to stable codes: TaskNotFoundError becomes 404 and "
        "InvalidTransitionError becomes 409, so clients always receive a consistent error JSON structure.",
        (
            "“Our API uses clear HTTP status codes: 404 for missing resources and 409 for invalid state transitions.”",
            "「我们的 API 使用清晰的 HTTP 状态码：404 表示资源不存在，409 表示非法状态转换。」",
        ),
    )

    _knowledge_point(
        doc,
        "Pydantic：请求数据的守门员",
        "Pydantic: the gatekeeper of request data",
        "Pydantic 是一个数据校验库。你声明字段的类型和规则（比如 title 必须是 1 到 200 个字符），Pydantic 会在数据"
        "进入业务逻辑之前自动检查。不合法就返回 422 错误；合法就给你一个类型正确、清洗过的对象。它的 extra=\"forbid\""
        "选项会拒绝一切没声明过的字段——比如客户端想偷偷传一个 status 字段，会被直接拒绝。",
        "Pydantic is a data validation library. You declare field types and rules — for example, title must be "
        "1 to 200 characters — and Pydantic checks the data before it reaches the business logic. Invalid data "
        "gets a 422 error; valid data becomes a clean, type-checked object. Its extra=\"forbid\" option rejects "
        "any undeclared field — for example, a client trying to sneak in a status field is refused outright.",
        [
            ("校验 — Validation", "Checking that data follows the declared rules"),
            ("Schema — Schema", "The declared shape and rules of the data"),
            ("拒绝额外字段 — Forbid extra fields", "Rejecting undeclared keys instead of ignoring them"),
            ("类型安全 — Type safety", "Guaranteeing the field has the declared type"),
        ],
        "backend/app/schemas/task.py 的 TaskCreate 使用 extra=\"forbid\" 和字段校验器：title 去除空格后不能为空，"
        "due_at 必须带时区。",
        "TaskCreate in backend/app/schemas/task.py uses extra=\"forbid\" and field validators: title must not be "
        "blank after stripping, and due_at must include a timezone offset.",
        (
            "“I use Pydantic schemas with extra='forbid' so clients cannot inject protected fields like status.”",
            "「我用带 extra='forbid' 的 Pydantic 模型，让客户端无法注入 status 之类的受保护字段。」",
        ),
    )

    _knowledge_point(
        doc,
        "SQLAlchemy：用 Python 对象操作数据库",
        "SQLAlchemy: working with the database through Python objects",
        "SQLAlchemy 是一个「对象关系映射」库（ORM，Object-Relational Mapping）。它让你不写 SQL 语句，而是定义 Python 类"
        "来表示数据库表。模型类（如 Task）的实例就是数据库里的一行，保存、查询、修改都通过 Python 完成。"
        "这样代码可读性好，也能在不同数据库之间切换。",
        "SQLAlchemy is an Object-Relational Mapping (ORM) library. Instead of writing SQL by hand, you define "
        "Python classes that represent database tables. An instance of a model class such as Task is one row "
        "in the database — saving, querying, and updating all happen through Python. This keeps the code "
        "readable and makes it easy to switch databases.",
        [
            ("对象关系映射 — ORM", "Mapping database rows to Python objects"),
            ("模型 — Model", "A Python class that represents a table"),
            ("会话 — Session", "A workspace for database operations"),
            ("映射列 — Mapped column", "A Python attribute tied to a table column"),
        ],
        "backend/app/models/task.py 定义了 Task 模型：id、title、description、status、due_at 和两个时间戳。"
        "services/task_service.py 通过 Session 完成查询和提交。",
        "backend/app/models/task.py defines the Task model: id, title, description, status, due_at, and two "
        "timestamps. services/task_service.py uses a Session to query and commit.",
        (
            "“I model the database with SQLAlchemy ORM, so the service layer works with Python objects instead of raw SQL.”",
            "「我用 SQLAlchemy ORM 建模数据库，服务层操作 Python 对象而不是手写 SQL。」",
        ),
    )

    _knowledge_point(
        doc,
        "SQLite：一个文件就是数据库",
        "SQLite: one file is the database",
        "SQLite 是最常见的嵌入式数据库：整个数据库就是一个文件，不需要安装独立的数据库服务器。它非常适合开发阶段"
        "和小型项目。因为数据库就是一个文件，测试时很容易给它一个临时路径，测完删掉，不会污染开发数据。"
        "它的一个小特点是需要 check_same_thread=False 才能在 FastAPI 的多线程环境下使用。",
        "SQLite is the most common embedded database: the whole database is one file, and no separate database "
        "server is needed. It is great for development and small projects. Because the database is a single "
        "file, tests can easily point to a temporary path and delete it afterwards without touching development "
        "data. One quirk: it needs check_same_thread=False to work under FastAPI's multi-threaded environment.",
        [
            ("嵌入式数据库 — Embedded database", "A database that lives inside the application"),
            ("数据库文件 — Database file", "The single file that stores all tables"),
            ("临时数据库 — Temporary database", "A throwaway database used only by tests"),
        ],
        "backend/app/core/database.py 用 settings.database_url 创建引擎；测试（tests/conftest.py）为每个测试创建"
        "独立的临时 SQLite 文件。",
        "backend/app/core/database.py creates the engine from settings.database_url; tests (tests/conftest.py) "
        "create an isolated temporary SQLite file for every test.",
        (
            "“We use SQLite in development because it needs no server, and tests use a temporary database file for isolation.”",
            "「开发环境我们用 SQLite，因为它不需要服务器；测试用临时数据库文件保证隔离。」",
        ),
    )

    _knowledge_point(
        doc,
        "Alembic：数据库版本的时光机",
        "Alembic: a time machine for database versions",
        "数据库的结构（表长什么样）会随项目演化。Alembic 是数据库迁移工具：每次结构变化写一个「迁移」文件，"
        "记录从旧结构到新结构的步骤。upgrade 把数据库向前推进，downgrade 可以回退。这样无论新机器还是老数据库，"
        "都能用同一套迁移把结构变得一致。",
        "Database structure evolves with the project. Alembic is a migration tool: every structural change is "
        "written as a migration describing the steps from the old structure to the new one. upgrade moves the "
        "database forward, and downgrade can roll it back. This way every machine — new or old — reaches the "
        "same schema through the same migrations.",
        [
            ("迁移 — Migration", "A versioned script that changes the schema"),
            ("升级 — Upgrade", "Apply the migration to move forward"),
            ("回滚 — Downgrade", "Revert the migration to move backward"),
            ("初始迁移 — Initial migration", "The first migration that builds the schema from scratch"),
        ],
        "backend/alembic/versions/ 下有两个迁移：c26f1e0161a6 创建 tasks 表，4756c9756074 在 Phase 1 增加"
        "conversations 与 messages 表。tests/test_migrations.py 验证升级、回滚往返。",
        "backend/alembic/versions/ contains two migrations: c26f1e0161a6 creates the tasks table, and "
        "4756c9756074 adds conversations and messages in Phase 1. tests/test_migrations.py verifies the "
        "upgrade/downgrade round trip.",
        (
            "“Schema changes go through Alembic migrations, and we have tests for both upgrade and downgrade.”",
            "「数据库结构变更都走 Alembic 迁移，我们为升级和回滚都写了测试。」",
        ),
    )

    _knowledge_point(
        doc,
        "状态机：任务只能按规则改变状态",
        "State machine: tasks can only change state by the rules",
        "状态机是一个概念：一个对象在任何时刻处于有限个状态之一，状态之间的转换受到严格限制。"
        "想象一部电梯：你只能在电梯里按楼层的规则，不能从电梯里直接飞到屋顶。任务也一样——"
        "只能 DRAFT → CONFIRMED → DONE，跳级或回退都是非法操作，后端会拒绝。",
        "A state machine is a concept: an object is in exactly one of a limited set of states at any time, and "
        "transitions between states are strictly restricted. Imagine an elevator: you can only press floor "
        "buttons inside it — you cannot fly from the elevator to the rooftop. Tasks work the same way: only "
        "DRAFT to CONFIRMED to DONE is allowed; skipping or reversing is an invalid operation that the "
        "backend rejects.",
        [
            ("状态机 — State machine", "A model where transitions follow fixed rules"),
            ("状态转换 — State transition", "Moving from one state to another"),
            ("非法操作 — Invalid operation", "An action the state machine forbids"),
            ("状态转换表 — Transition table", "A table that lists which transitions are allowed"),
        ],
        "backend/app/services/task_service.py 中的 ALLOWED_TRANSITIONS 字典就是转换表：confirm 只允许 DRAFT 出发，"
        "complete 只允许 CONFIRMED 出发。",
        "The ALLOWED_TRANSITIONS dictionary in backend/app/services/task_service.py is the transition table: "
        "confirm may only start from DRAFT, and complete may only start from CONFIRMED.",
        (
            "“I implemented a state machine to prevent invalid task transitions, so a draft can never be completed directly.”",
            "「我实现了状态机来阻止非法的任务状态转换，草稿永远不能直接变成已完成。」",
        ),
    )

    _knowledge_point(
        doc,
        "DRAFT 确认机制：先草稿，再确认",
        "The DRAFT confirmation mechanism: draft first, confirm later",
        "为什么任务创建出来必须是 DRAFT？因为创建和确认是两个不同语义的动作：创建只是记录意图，确认才是正式生效。"
        "这种「两步走」设计让系统可以在确认时挂载副作用（比如以后生成日历事件或提醒），而创建阶段保持零副作用——"
        "创建任务永远不会产生日历或提醒。确认是独立接口，不能通过创建时传一个字段来冒充。",
        "Why must a created task be a DRAFT? Because creation and confirmation are two different actions: "
        "creation only records intent, while confirmation makes the task official. This two-step design lets "
        "the system attach side effects to confirmation later — such as calendar events or reminders — while "
        "creation stays side-effect-free. Confirmation is a separate endpoint; you cannot fake it by sending "
        "a status field at creation time.",
        [
            ("草稿 — Draft", "An unconfirmed, not-yet-active record"),
            ("确认 — Confirm", "The action that activates a draft"),
            ("副作用 — Side effect", "An external action caused by a request"),
            ("独立接口 — Separate endpoint", "A dedicated route with its own semantics"),
        ],
        "TaskCreate schema 没有 status 字段且 extra=\"forbid\"；POST /api/tasks/{id}/confirm 是唯一的确认入口。",
        "TaskCreate has no status field and uses extra=\"forbid\"; POST /api/tasks/{id}/confirm is the only "
        "confirmation entry point.",
        (
            "“Creation always produces a DRAFT. Confirmation is a separate endpoint, so side effects can be attached there later.”",
            "「创建永远产生草稿。确认是独立接口，以后可以把副作用挂载在那里。」",
        ),
    )

    _knowledge_point(
        doc,
        "UTC：全世界统一的时间标准",
        "UTC: the unified time standard",
        "UTC（协调世界时，Coordinated Universal Time）是世界统一的时间基准。如果每个地区按自己的本地时间存数据库，"
        "跨时区对账就会混乱。JARVIS 的约定是：数据库里只存 UTC；API 输入必须带时区（比如 +08:00），不能提交没有时区的"
        "时间；API 输出统一带 Z 结尾（Z 就是 UTC 的缩写）。前端显示时再转换成本地时间。",
        "UTC (Coordinated Universal Time) is the world's unified time base. If every region stored its own local "
        "time, cross-timezone comparisons would be a mess. JARVIS's rule: the database stores UTC only; API "
        "input must include a timezone offset such as +08:00 — naive timestamps are rejected; API output always "
        "ends with Z (Z stands for UTC). The frontend converts to local time for display.",
        [
            ("时区 — Timezone", "A region's offset from UTC"),
            ("无时区时间 — Naive datetime", "A timestamp with no timezone information"),
            ("UTC 后缀 — The Z suffix", "Indicates the timestamp is UTC"),
            ("转换 — Conversion", "Changing a timestamp between timezones"),
        ],
        "backend/app/schemas/common.py 集中实现 serialize_utc；测试断言写入 +08:00 后 API 返回 01:00:00Z。",
        "backend/app/schemas/common.py centralizes serialize_utc; tests assert that writing +08:00 returns "
        "01:00:00Z from the API.",
        (
            "“All timestamps are stored in UTC. The API rejects naive datetimes instead of guessing the timezone.”",
            "「所有时间都以 UTC 存储。API 拒绝无时区的时间，而不是猜测时区。」",
        ),
    )

    _knowledge_point(
        doc,
        "CORS：允许哪个前端访问",
        "CORS: which frontend is allowed to call",
        "浏览器的安全机制默认禁止网页访问不同来源的接口。开发时前端跑在 localhost:5173，后端在 localhost:8000，"
        "来源不同，所以后端必须明确声明允许的来源。CORS（跨源资源共享，Cross-Origin Resource Sharing）就是这份声明。"
        "JARVIS 把白名单放进环境变量，不写死，也不允许同时开启通配来源和 credentials。",
        "Browser security prevents a web page from calling APIs on a different origin by default. In development "
        "the frontend runs on localhost:5173 and the backend on localhost:8000 — different origins — so the "
        "backend must explicitly declare allowed origins. CORS (Cross-Origin Resource Sharing) is that "
        "declaration. JARVIS keeps the whitelist in environment variables, never hard-coded, and never enables "
        "wildcard origins together with credentials.",
        [
            ("来源 — Origin", "Scheme + host + port of a web page"),
            ("白名单 — Whitelist", "The list of allowed origins"),
            ("预检请求 — Preflight request", "A CORS check the browser sends first"),
        ],
        "backend/app/core/config.py 的 cors_origins 从环境变量 CORS_ORIGINS 读取，main.py 中显式"
        "allow_credentials=False。",
        "cors_origins in backend/app/core/config.py comes from the CORS_ORIGINS environment variable, and "
        "main.py sets allow_credentials=False explicitly.",
        (
            "“CORS origins come from an environment variable, and we never combine wildcard origins with credentials.”",
            "「CORS 来源从环境变量读取，我们绝不把通配来源和 credentials 组合使用。」",
        ),
    )

    _knowledge_point(
        doc,
        "自动化测试：让代码永远保持正确",
        "Automated tests: keeping the code correct forever",
        "测试是一段自动运行的代码，它替你检查程序的行为是否符合预期。pytest 是 Python 最流行的测试框架。"
        "JARVIS 的测试是真实 API 测试：用 FastAPI 的 TestClient 直接发请求，验证状态码、错误结构、时区转换、"
        "数据库持久化。测试必须用独立的临时数据库，绝对不能污染开发数据。",
        "A test is automated code that checks whether the program behaves as expected. pytest is the most "
        "popular Python testing framework. JARVIS tests are real API tests: they use FastAPI's TestClient to "
        "send actual requests and verify status codes, error structures, timezone conversion, and database "
        "persistence. Tests must use isolated temporary databases and never touch development data.",
        [
            ("测试框架 — Test framework", "A tool that runs and reports tests"),
            ("测试夹具 — Fixture", "Setup that prepares the test environment"),
            ("断言 — Assertion", "A check that a condition is true"),
            ("隔离 — Isolation", "Keeping tests independent of each other"),
        ],
        "backend/tests/test_tasks.py 有 30 个 API 测试，tests/conftest.py 用 tmp_path 为每个测试创建临时 SQLite。",
        "backend/tests/test_tasks.py contains 30 API tests, and tests/conftest.py creates a temporary SQLite "
        "database per test using tmp_path.",
        (
            "“We have 54 automated API tests that cover state transitions, timezone rules, and database isolation.”",
            "「我们有 54 个自动化 API 测试，覆盖状态转换、时区规则和数据库隔离。」",
        ),
    )

    # ---------------- 核心代码讲解 ----------------
    doc.add_heading("核心代码讲解", "Core code explained", level=1)
    doc.add_pair(
        "下面三段代码是 Phase 0 最核心的实现。请先阅读真实代码文件，再对照讲解。",
        "The three snippets below are the heart of Phase 0. Read the real files first, then follow the explanation.",
    )

    doc.add_code_explain(
        {
            "location": "backend/app/services/task_service.py — _transition 函数",
            "code": '''ALLOWED_TRANSITIONS: dict[str, dict[TaskStatus, TaskStatus]] = {
    "confirm": {TaskStatus.DRAFT: TaskStatus.CONFIRMED},
    "complete": {TaskStatus.CONFIRMED: TaskStatus.DONE},
}


def _transition(db: Session, task_id: int, action: str) -> Task:
    task = get_task(db, task_id)
    transitions = ALLOWED_TRANSITIONS[action]
    current = TaskStatus(task.status)
    if current not in transitions:
        allowed = ", ".join(s.value for s in transitions)
        raise InvalidTransitionError(task_id, action, current, allowed)
    task.status = transitions[current].value
    db.commit()
    db.refresh(task)
    return task''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "这个函数是状态机的核心：先查任务是否存在，再看当前状态是否允许执行该动作，允许就改状态并提交数据库，不允许就抛出 InvalidTransitionError。",
                    "en": "This function is the heart of the state machine: it loads the task, checks whether "
                    "the current state permits the requested action, updates the status and commits if allowed, "
                    "and raises InvalidTransitionError otherwise.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("先用 get_task 读取任务，任务不存在会先抛出 404 异常。", "get_task loads the task; a missing task raises the 404 exception first."),
                        ("从转换表取出该动作允许的「当前状态 → 目标状态」映射。", "The transition table is looked up to get the allowed current-to-target mapping for this action."),
                        ("把数据库里的状态字符串转成枚举，检查是否在允许的出发状态里。", "The stored status string is converted to an enum and checked against the allowed starting states."),
                        ("不在表里就抛 InvalidTransitionError，由 API 层变成 409 响应。", "If the current state is not in the table, InvalidTransitionError is raised and the API layer turns it into a 409 response."),
                        ("在表里就把状态改成目标值，提交并刷新对象。", "Otherwise the status is set to the target value, committed, and refreshed."),
                    ],
                    "en_intro": "Step by step: first the task is loaded; then the allowed mapping for this action is "
                    "looked up; the current status is checked against it; an invalid state raises "
                    "InvalidTransitionError; a valid state is updated and committed.",
                },
                {
                    "kind": "why",
                    "cn": "把转换规则集中放在一个字典里，规则一目了然，新增动作只需加一行。业务规则放在服务层、不感知 HTTP，"
                    "意味着测试可以纯函数式地验证，也可以被未来任何接口复用。",
                    "en": "Centralizing the rules in one dictionary makes them readable, and adding an action is one "
                    "line. Keeping rules in the service layer, away from HTTP, lets tests verify the logic "
                    "directly and lets future endpoints reuse it.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是在路由层直接写 if task.status == ... 判断，规则散落各处，改一条规则要改多个文件，"
                    "而且测试难以覆盖。另一个常见错误是忘记 commit，状态改了但没写进数据库。",
                    "en": "A common mistake is writing if task.status == ... checks directly in route handlers, "
                    "scattering the rules across files and making them hard to test. Another is forgetting to "
                    "commit, so the change never reaches the database.",
                },
            ],
        }
    )

    doc.add_code_explain(
        {
            "location": "backend/app/schemas/task.py — TaskCreate",
            "code": '''class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    due_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "这个 schema 定义创建任务请求的合法形状：只有 title、description、due_at 三个字段，"
                    "extra=\"forbid\" 拒绝任何其他字段，title 去掉首尾空格后必须非空。",
                    "en": "This schema defines the legal shape of a create-task request: only title, description, "
                    "and due_at. extra=\"forbid\" rejects any other field, and title must not be blank after "
                    "stripping whitespace.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("声明 extra=\"forbid\"：请求里出现未声明字段直接 422。", "extra=\"forbid\" is declared, so any undeclared field in the request causes a 422."),
                        ("声明字段类型与长度上限，Pydantic 自动检查。", "Field types and length limits are declared; Pydantic checks them automatically."),
                        ("field_validator 钩子先去除首尾空格，再检查是否为空。", "The field_validator hook strips whitespace first, then checks for emptiness."),
                    ],
                    "en_intro": "The model declares forbidden extras, typed fields with limits, and a validator that "
                    "strips and rejects blank titles.",
                },
                {
                    "kind": "why",
                    "cn": "输入校验必须在最外层完成：未经验证的数据一旦进入业务逻辑，状态机和安全边界都可能被绕过。"
                    "没有 status 字段，从根源上保证创建只能产生 DRAFT。",
                    "en": "Validation must happen at the outermost layer: unvalidated data reaching business logic "
                    "could bypass the state machine and security boundaries. With no status field, creation can "
                    "only ever produce a DRAFT.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是用 min_length=1 检查空白：字符串 “   ” 长度是 3，能通过检查。"
                    "必须先 strip 再判断是否为空。",
                    "en": "A common mistake is relying on min_length=1 to reject blanks: the string \"   \" has "
                    "length 3 and passes. You must strip first, then check for emptiness.",
                },
            ],
        }
    )

    doc.add_code_explain(
        {
            "location": "backend/app/main.py — 统一错误结构",
            "code": '''@app.exception_handler(InvalidTransitionError)
async def invalid_transition_handler(_: Request, exc: InvalidTransitionError):
    return JSONResponse(status_code=409, content=_error_body("INVALID_TRANSITION", str(exc)))


@app.exception_handler(Exception)
async def unhandled_exception_handler(_: Request, exc: Exception):
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=_error_body("INTERNAL_ERROR", "Internal server error"),
    )''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "这两个处理器把服务层异常转换成统一结构的 JSON 响应：业务异常映射为 409，未知异常映射为 500，"
                    "并且堆栈只进日志，绝不发给客户端。",
                    "en": "These handlers convert service exceptions into uniformly structured JSON responses: "
                    "business errors become 409, unknown errors become 500, and the stack trace only goes to "
                    "the logs, never to the client.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("FastAPI 抛出异常时查找已注册的 exception_handler。", "When an exception propagates, FastAPI looks up a registered handler for its type."),
                        ("InvalidTransitionError 被捕获并包装成 409 + 错误码。", "InvalidTransitionError is caught and wrapped into 409 with an error code."),
                        ("其他未处理异常落到兜底处理器：日志记录细节，客户端只收到稳定文案。", "Any other unhandled exception falls to the catch-all handler: details go to the log, the client gets a stable message."),
                    ],
                    "en_intro": "FastAPI dispatches the exception to its handler; business errors become 409; "
                    "unknown errors are logged and answered with a stable 500 message.",
                },
                {
                    "kind": "why",
                    "cn": "统一错误结构让前端只需解析一种格式；稳定文案防止泄露堆栈、数据库内部信息或供应商响应，"
                    "这是安全边界的一部分。",
                    "en": "A unified error structure means the frontend parses one format only; stable messages "
                    "prevent leaking stack traces, database internals, or vendor responses — part of the "
                    "security boundary.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是让框架默认 500 响应直接暴露调试信息，或在错误消息里拼接原始异常字符串。"
                    "另一个常见错误是忘记注册 handler，导致同一种错误在不同路径返回不同格式。",
                    "en": "A common mistake is letting the default 500 response expose debug information, or "
                    "concatenating raw exception strings into messages. Another is forgetting to register a "
                    "handler, so the same error returns different shapes on different paths.",
                },
            ],
        }
    )

    # ---------------- 技术英语 ----------------
    doc.add_heading("Technical English 技术英语", "Technical English vocabulary", level=1)
    doc.add_pair(
        "以下 20 个核心词汇覆盖 Phase 0 的全部关键技术概念。每个词条给出中文含义、简单的英文定义、"
        "JARVIS 中的真实例子和常见搭配。",
        "The 20 core terms below cover every key concept of Phase 0. Each entry gives the Chinese meaning, a "
        "simple English definition, a real JARVIS example, and common phrases.",
    )
    vocab = [
        ("State machine", "状态机", "A model with fixed transition rules",
         "A model in which an object can only move between states according to fixed rules.",
         "Tasks only move DRAFT → CONFIRMED → DONE.",
         "implement a state machine, define transitions"),
        ("State transition", "状态转换", "Moving from one state to another",
         "The act of moving an object from one state to another.",
         "confirm_task() performs the DRAFT → CONFIRMED transition.",
         "perform a transition, reject a transition"),
        ("Invalid operation", "非法操作", "An action forbidden by the rules",
         "An action that the rules forbid in the current state.",
         "Completing a DRAFT task returns 409 INVALID_TRANSITION.",
         "reject an invalid operation, handle an invalid case"),
        ("Endpoint", "接口/端点", "A URL that accepts requests",
         "A URL that accepts requests and returns responses.",
         "POST /api/tasks/{id}/confirm is an endpoint.",
         "define an endpoint, call an endpoint"),
        ("Request body", "请求体", "Data sent with a request",
         "The JSON data a client sends with a request.",
         "The frontend sends {title, description, due_at} in the body.",
         "parse the request body, validate the body"),
        ("Status code", "状态码", "A number summarizing the result",
         "A three-digit number summarizing the result of a request.",
         "201 for created, 404 for missing, 409 for conflict.",
         "return a status code, map errors to codes"),
        ("Validation", "校验", "Checking input against rules",
         "Checking that input follows the declared rules before use.",
         "Pydantic validates title length and timezone awareness.",
         "validate input, fail validation"),
        ("Schema", "数据模型/结构定义", "The declared shape of data",
         "The declared structure and rules of the data.",
         "TaskCreate declares the three allowed fields.",
         "define a schema, validate against a schema"),
        ("Extra fields", "额外字段", "Fields not declared in the schema",
         "Fields that were not declared in the schema.",
         "extra=\"forbid\" rejects extra fields like status.",
         "forbid extra fields, ignore extra fields"),
        ("ORM", "对象关系映射", "Mapping rows to objects",
         "A layer that maps database rows to Python objects.",
         "SQLAlchemy maps the Task class to the tasks table.",
         "use an ORM, map a model to a table"),
        ("Model", "模型", "A class representing a table",
         "A Python class representing a database table.",
         "Task in models/task.py has seven columns.",
         "define a model, add a column"),
        ("Migration", "迁移", "A versioned schema change",
         "A versioned script that changes the database schema.",
         "Two migrations build tasks, conversations and messages.",
         "run a migration, write a migration"),
        ("Downgrade", "回滚", "Reverting to an older schema",
         "Reverting a migration to an older schema.",
         "downgrade drops the conversations and messages tables.",
         "test the downgrade, roll back"),
        ("UTC", "协调世界时", "The global time standard",
         "The global time standard; the database stores only UTC.",
         "due_at is stored as 2026-08-10T01:00:00Z.",
         "store in UTC, convert to UTC"),
        ("Naive datetime", "无时区时间", "A timestamp without timezone",
         "A timestamp without timezone information.",
         "The API rejects naive datetimes with a 422 error.",
         "reject naive datetimes, assume UTC"),
        ("Timezone offset", "时区偏移", "Local time minus UTC",
         "The difference between local time and UTC.",
         "+08:00 means the local time is eight hours ahead of UTC.",
         "include an offset, convert the offset"),
        ("CORS", "跨源资源共享", "Cross-origin access control",
         "A browser security mechanism controlling which origins may call.",
         "CORS_ORIGINS lists localhost:5173 and 127.0.0.1:5173.",
         "configure CORS, allow an origin"),
        ("Whitelist", "白名单", "A list of allowed items",
         "A list of explicitly allowed items.",
         "The CORS origin whitelist comes from an environment variable.",
         "maintain a whitelist, add an origin"),
        ("Fixture", "测试夹具", "Test setup code",
         "Setup code that prepares the test environment.",
         "conftest.py creates a temporary database per test.",
         "define a fixture, use a fixture"),
        ("Isolation", "隔离", "Keeping tests independent",
         "Keeping tests independent so they cannot affect each other.",
         "Tests use temporary databases; the dev database stays empty.",
         "ensure isolation, achieve isolation"),
    ]
    doc.add_table(
        ("术语 / Term", "含义 / Meaning", "定义 / Simple definition", "JARVIS 实例 / Example", "常见搭配 / Phrases"),
        [[cn, en_meaning, definition, example, phrases] for term, cn, en_meaning, definition, example, phrases in vocab],
        widths=[2.9, 2.1, 4.6, 3.4, 3.0],
    )

    # ---------------- Reading Code Aloud ----------------
    doc.add_heading("Reading Code Aloud 代码口头表达", "How to describe code in English", level=1)
    doc.add_pair(
        "面试或写技术博客时，你需要用英语描述代码做了什么。下面是 5 段真实代码的口头表达示范。",
        "In interviews or technical writing, you need to describe what code does in English. Below are five "
        "spoken descriptions of real JARVIS code.",
    )
    reading = [
        (
            "任务状态机（task_service.py）",
            "“This endpoint validates the request and delegates the business logic to the service layer.”",
            "「这个接口校验请求，并把业务逻辑委托给服务层。」",
            "转换表把每个动作映射到允许的转换；当前状态不被允许时，服务层会抛出异常。",
            "The transition table maps each action to its allowed transitions; the service raises an error "
            "when the current state is not allowed.",
        ),
        (
            "创建任务路由（api/tasks.py）",
            "“The route parses the Pydantic payload, calls the service to create the task, and returns a 201 response.”",
            "「路由解析 Pydantic 请求体，调用服务创建任务，并返回 201 响应。」",
            "FastAPI 把解析好的请求体和数据库会话作为依赖注入到路由函数里。",
            "FastAPI injects the parsed payload and the database session as dependencies.",
        ),
        (
            "时区序列化（schemas/common.py）",
            "“The serializer converts every timestamp to UTC and appends a capital Z so the client knows it is UTC.”",
            "「序列化器把所有时间转换为 UTC 并加上大写的 Z，客户端就知道这是 UTC 时间。」",
            "从 SQLite 读回的时间可能是无时区的，所以序列化器先假定为 UTC 再格式化。",
            "Values read back from SQLite may be naive, so the serializer assumes UTC before formatting.",
        ),
        (
            "测试夹具（conftest.py）",
            "“The fixture creates a temporary SQLite database for each test and overrides the dependency, so the dev database is never touched.”",
            "「夹具为每个测试创建临时 SQLite 数据库并覆盖依赖，开发库永远不会被触碰。」",
            "依赖覆盖（dependency override）在测试期间替换掉真正的 get_db 函数。",
            "Dependency overrides replace the real get_db function during tests.",
        ),
        (
            "错误映射（main.py）",
            "“The exception handler converts the domain error into a 409 response with a stable error code, and logs the details server-side.”",
            "「异常处理器把领域错误转换成带稳定错误码的 409 响应，细节只记在服务端日志里。」",
            "兜底处理器保证客户端永远看不到堆栈信息。",
            "The catch-all handler guarantees the client never sees a stack trace.",
        ),
    ]
    for title, sentence, meaning, note_cn, note_en in reading:
        doc.add_heading(title, title, level=3)
        doc.add_pair(sentence, meaning)
        doc.add_label("延伸说明 / Extended note")
        doc.add_pair(note_cn, note_en)

    # ---------------- Interview English ----------------
    doc.add_heading("Interview English 面试英语", "Interview practice", level=1)
    doc.add_pair(
        "以下 10 个问题覆盖 Phase 0 的高频面试点。先自己回答，再对照参考回答。",
        "These ten questions cover the common interview points of Phase 0. Answer by yourself first, then "
        "compare with the reference answers.",
    )
    interviews = [
        (
            "为什么任务创建出来必须是 DRAFT？",
            "Why must a created task always be a DRAFT?",
            "因为创建和确认是两个不同语义的动作。创建阶段保持零副作用，确认才是正式生效的入口，未来可以在确认时挂载日历或提醒等副作用。",
            "Creation and confirmation are two different actions. Creation stays side-effect-free, and "
            "confirmation is the official activation point where future side effects like calendar or "
            "reminders can be attached.",
            "side-effect-free, official activation point",
        ),
        (
            "如何防止非法的状态转换？",
            "How do you prevent invalid state transitions?",
            "我实现了一个状态机：转换规则集中在一个转换表里，服务层检查当前状态是否在允许的出发状态中，非法转换抛异常，API 层映射为 409。",
            "I implemented a state machine: transition rules live in one table, the service layer checks "
            "whether the current state is an allowed starting state, invalid transitions raise an exception, "
            "and the API layer maps it to a 409.",
            "allowed starting state, raise an exception",
        ),
        (
            "为什么所有时间都用 UTC 存储？",
            "Why do you store all timestamps in UTC?",
            "UTC 是世界统一的时间基准。混合存储本地时间会导致跨时区对账混乱。API 要求输入带时区，拒绝无时区时间，避免静默猜测时区带来的错误。",
            "UTC is the global time base. Mixing local times causes cross-timezone chaos. The API requires "
            "timezone-aware input and rejects naive datetimes, so we never silently guess a timezone.",
            "silently guess, timezone-aware",
        ),
        (
            "extra=“forbid” 解决了什么问题？",
            "What problem does extra=\"forbid\" solve?",
            "它保证未声明的字段直接返回 422，而不是被静默忽略。这样客户端无法通过多传 status 等字段绕过安全边界。",
            "It guarantees that undeclared fields get a 422 instead of being silently ignored, so a client "
            "cannot bypass the security boundary by injecting fields like status.",
            "bypass the boundary, silently ignore",
        ),
        (
            "测试如何做到数据库隔离？",
            "How do your tests achieve database isolation?",
            "每个测试夹具创建一个独立的临时 SQLite 文件，并通过依赖覆盖替换数据库会话。测试数据只进临时库，开发库始终保持零行。",
            "Each fixture creates an isolated temporary SQLite file and replaces the database session via "
            "dependency overrides. Test data only reaches the temporary database, and the dev database "
            "stays at zero rows.",
            "dependency override, temporary database",
        ),
        (
            "统一错误结构有什么好处？",
            "What are the benefits of a unified error structure?",
            "前端只需要解析一种格式；错误码稳定可枚举，比如 TASK_NOT_FOUND、INVALID_TRANSITION；同时稳定文案避免泄露堆栈和内部细节。",
            "The frontend parses one format only; error codes are stable and enumerable, such as "
            "TASK_NOT_FOUND or INVALID_TRANSITION; and stable messages avoid leaking stack traces or "
            "internal details.",
            "stable error codes, enumerable",
        ),
        (
            "Alembic 在你的项目里扮演什么角色？",
            "What role does Alembic play in your project?",
            "Alembic 管理数据库结构的版本。每次结构变化都生成一个迁移文件，upgrade 和 downgrade 都有测试，保证新旧环境结构一致且可回滚。",
            "Alembic versions the database schema. Every structural change becomes a migration file, both "
            "upgrade and downgrade are tested, so every environment reaches the same schema and can roll back.",
            "version the schema, roll back",
        ),
        (
            "开发时 CORS 是怎么配置的？",
            "How is CORS configured in development?",
            "前端在 5173 端口，后端在 8000 端口，来源不同。后端的来源白名单从环境变量读取，默认包含两个本地开发地址，并且显式关闭 credentials。",
            "The frontend runs on port 5173 and the backend on 8000 — different origins. The backend reads "
            "its origin whitelist from an environment variable, defaults to the two local dev addresses, "
            "and explicitly disables credentials.",
            "origin whitelist, explicit default",
        ),
        (
            "为什么用 API 测试而不是纯单元测试？",
            "Why API tests instead of pure unit tests?",
            "API 测试覆盖完整调用链：路由、校验、业务规则、持久化和错误映射，模拟真实客户端行为，回归时更接近上线后的表现。",
            "API tests cover the whole chain: routing, validation, business rules, persistence, and error "
            "mapping. They simulate real client behavior, so regressions resemble what would happen in "
            "production.",
            "full call chain, simulate real behavior",
        ),
        (
            "如果让你扩展这个状态机，你会怎么做？",
            "How would you extend this state machine?",
            "我会先在转换表里加一行新的动作映射，再为新动作写路由和服务函数，最后补测试覆盖合法与非法路径。规则保持集中，扩展点最小。",
            "I would add a new action to the transition table, write the route and service function for it, "
            "then add tests covering both legal and illegal paths. Rules stay centralized and the extension "
            "point stays minimal.",
            "centralized rules, minimal extension point",
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

    # ---------------- Sixty-second Introduction ----------------
    doc.add_heading("Sixty-second Introduction 一分钟项目介绍", "Introducing JARVIS in one minute", level=1)
    doc.add_pair(
        "一分钟介绍是面试的第一道题。目标是 60 秒内讲清楚：项目是什么、你负责什么、技术亮点是什么。",
        "The one-minute introduction is the first question in an interview. The goal is to explain in 60 "
        "seconds: what the project is, what you built, and what the technical highlights are.",
    )
    intro_cn = (
        "JARVIS 是一个个人 AI 助理项目，目前完成了两个阶段。Phase 0 是任务管理系统：我用 FastAPI 和 SQLAlchemy "
        "构建了 REST API，用 SQLite 持久化，任务走严格的状态机——草稿、确认、完成，非法转换会被拒绝。所有时间统一 "
        "UTC，API 拒绝无时区输入。Phase 1 加入了大模型网关：我设计了与供应商解耦的 Provider 抽象，OpenAI 兼容 "
        "实现与 Fake Provider 可切换，对话消息持久化，超时和错误有统一映射。整个项目有 54 个自动化测试，包括迁移 "
        "升级回滚测试。我负责全部后端设计、测试和前端界面。"
    )
    intro_en = (
        "JARVIS is a personal AI assistant project with two phases so far. In Phase 0 I built a task management "
        "system: a REST API with FastAPI and SQLAlchemy, SQLite for persistence, and a strict state machine — "
        "draft, confirm, done — that rejects invalid transitions. All timestamps are UTC, and the API rejects "
        "naive datetimes. In Phase 1 I added an LLM gateway: a vendor-agnostic provider abstraction with an "
        "OpenAI-compatible implementation and a Fake provider for testing, persistent conversations, and "
        "unified timeout and error mapping. The project has 54 automated tests, including migration "
        "upgrade and downgrade tests. I own the backend design, the tests, and the frontend."
    )
    doc.add_label("中文版本")
    doc.add_cn_only(intro_cn)
    doc.add_label("English version")
    doc.add_en_only(intro_en)
    doc.add_pair(
        "断句提示：分四句讲——① 项目是什么；② Phase 0 做了什么（状态机、UTC）；③ Phase 1 做了什么（Provider 抽象、"
        "Fake、错误映射）；④ 测试数量与你负责的范围。建议强调的重音词：state machine、UTC、vendor-agnostic、"
        "provider abstraction、54 automated tests。",
        "Pacing: four sentences — ① what the project is; ② what Phase 0 built (state machine, UTC); ③ what "
        "Phase 1 built (provider abstraction, Fake, error mapping); ④ test count and your scope. Stress these "
        "words: state machine, UTC, vendor-agnostic, provider abstraction, 54 automated tests.",
    )

    # ---------------- Writing Practice ----------------
    doc.add_heading("Writing Practice 写作练习", "Writing practice", level=1)
    doc.add_pair(
        "以下 5 题请用英文作答，答案在下一节。先自己写，再对照。",
        "Answer these five questions in English; reference answers are in the next section. Write your own "
        "first, then compare.",
    )
    writing = [
        "Explain in English why a task is created as a DRAFT first, and why confirmation is a separate endpoint.",
        "Explain in English what the FakeLLMProvider is and why automated tests must use it instead of a real model.",
        "Write an API error report in English: a client receives a 409 INVALID_TRANSITION when confirming a DONE task. Describe the request, the response, and the fix.",
        "Explain in English how JARVIS guarantees that all timestamps are UTC and why naive datetimes are rejected.",
        "Explain in English how the tests avoid touching the development database.",
    ]
    for i, q in enumerate(writing, start=1):
        doc.add_heading(f"写作题 {i}", f"Writing task {i}", level=3)
        doc.add_en_only(q)

    doc.add_heading("写作练习参考答案", "Writing practice reference answers", level=1)
    answers = [
        (
            "A task starts as a DRAFT because creation only records intent; it should not take effect by itself. "
            "Confirmation is the official activation step, so side effects such as reminders can be attached "
            "there later. Keeping confirmation as a separate endpoint also means the creation request can never "
            "fake a confirmed status.",
            "任务先创建为草稿，因为创建只是记录意图，不应自动生效。确认是正式生效步骤，以后可以在那里挂载提醒等副作用。确认作为独立接口也意味着创建请求永远无法伪造已确认状态。",
        ),
        (
            "The FakeLLMProvider simulates a model locally: it returns a fixed reply and records the messages it "
            "receives, and it can be configured to raise errors. Automated tests must use it so they never call "
            "a real external API, which would be slow, expensive, and nondeterministic.",
            "Fake Provider 在本地模拟模型：返回固定回复、记录收到的消息，还可以配置成抛出错误。自动化测试必须用它，绝不调用真实外部 API——那会慢、花钱且结果不确定。",
        ),
        (
            "Request: POST /api/tasks/1/confirm on a task that is already DONE. Response: HTTP 409 with "
            "{\"error\": {\"code\": \"INVALID_TRANSITION\", \"message\": \"...\"}}. The server refuses because the "
            "state machine only allows confirm from DRAFT. Fix: the frontend should hide the confirm button "
            "for DONE tasks, and the client can rely on the 409 as a final guard.",
            "请求：对已经是 DONE 的任务调用 POST /api/tasks/1/confirm。响应：HTTP 409 及统一错误结构。服务器拒绝是因为状态机只允许从 DRAFT 出发确认。修复：前端对 DONE 任务隐藏确认按钮，客户端以 409 作为最终防线。",
        ),
        (
            "Every timestamp is stored in UTC, and the serializer appends a capital Z so the output always "
            "states UTC explicitly. Inputs must carry a timezone offset; naive datetimes are rejected with a "
            "422 instead of being silently assigned a timezone, because guessing is a source of bugs.",
            "所有时间都以 UTC 存储，序列化时统一加 Z 后缀，输出始终明确为 UTC。输入必须携带时区偏移；无时区时间直接 422，而不是被静默赋予时区——猜测是 bug 的来源。",
        ),
        (
            "The conftest fixture creates a brand-new temporary SQLite file for each test and overrides the "
            "get_db dependency, so every request in the test talks to the temporary database. After the test "
            "the file is discarded, and a dedicated test asserts that the development database still has "
            "zero rows.",
            "conftest 夹具为每个测试创建全新的临时 SQLite 文件，并通过依赖覆盖替换 get_db，测试里的所有请求都访问临时库。测试结束后文件被丢弃，还有一个专门的测试断言开发库仍然零行。",
        ),
    ]
    for i, (en, cn) in enumerate(answers, start=1):
        doc.add_heading(f"参考答案 {i}", f"Reference answer {i}", level=3)
        doc.add_en_only(en)
        doc.add_cn_only(cn)

    # ---------------- Speaking Practice ----------------
    doc.add_heading("Speaking Practice 口语练习", "Speaking practice", level=1)
    doc.add_pair(
        "从一句话回答练到一分钟表达。先用 30 秒准备，再录音回听。",
        "Practice from one-sentence answers to a one-minute explanation. Prepare for 30 seconds, then record "
        "yourself and listen back.",
    )
    speaking = [
        ("一句话版：用一句英文说出状态机的核心作用。",
         "One-sentence version: say the core purpose of the state machine in one English sentence.",
         "The state machine guarantees that a task can only move through legal transitions such as from DRAFT to CONFIRMED.",
         "状态机保证任务只能走合法转换，比如从草稿到已确认。"),
        ("一句话版：用一句英文解释为什么拒绝无时区时间。",
         "One-sentence version: explain in one sentence why naive datetimes are rejected.",
         "We reject naive datetimes because silently guessing a timezone would corrupt cross-timezone data.",
         "我们拒绝无时区时间，因为静默猜测时区会破坏跨时区数据的正确性。"),
        ("30 秒版：描述 JARVIS 的分层架构。",
         "30-second version: describe the layered architecture of JARVIS.",
         "The API layer parses requests and maps errors; the service layer holds the state machine; the model layer handles persistence; tests use isolated temporary databases.",
         "API 层解析请求并映射错误；服务层持有状态机；模型层负责持久化；测试使用隔离的临时数据库。"),
        ("30 秒版：解释 Pydantic 的 extra=“forbid”。",
         "30-second version: explain Pydantic's extra=\"forbid\".",
         "extra=\"forbid\" makes validation strict: undeclared fields are rejected with a 422, so clients cannot inject protected fields such as status.",
         "extra=\"forbid\" 让校验变严格：未声明的字段以 422 被拒绝，客户端无法注入 status 之类的受保护字段。"),
        ("一分钟版：向一个不熟悉后端的人介绍你如何保证任务数据永远正确。",
         "One-minute version: explain to someone unfamiliar with backends how you keep task data correct.",
         "First, every request is validated by Pydantic. Second, the state machine only allows legal transitions and returns 409 otherwise. Third, all timestamps are UTC. Fourth, 54 automated tests run on isolated databases and catch regressions.",
         "第一，每个请求都经过 Pydantic 校验。第二，状态机只允许合法转换，否则返回 409。第三，所有时间都是 UTC。第四，54 个自动化测试在隔离数据库上运行，捕获回归。"),
    ]
    for i, (cn, en, answer, meaning) in enumerate(speaking, start=1):
        doc.add_heading(f"口语练习 {i}", f"Speaking practice {i}", level=3)
        doc.add_pair(cn, en)
        doc.add_label("示范回答")
        doc.add_en_only(answer)
        doc.add_cn_only(meaning)

    # ---------------- 术语表 ----------------
    doc.add_heading("术语速查表", "Quick glossary", level=1)
    doc.add_pair(
        "本表汇总文档中出现的重要术语。首次出现时使用全称加缩写（如 应用程序编程接口（Application Programming Interface, API）），"
        "后续正文直接使用缩写。",
        "This table summarizes the important terms. The first occurrence uses the full name plus the "
        "abbreviation — for example, Application Programming Interface (API) — and later paragraphs use "
        "the abbreviation directly.",
    )
    glossary = [
        ("API", "应用程序编程接口（Application Programming Interface）", "程序之间对话的约定"),
        ("HTTP", "超文本传输协议（HyperText Transfer Protocol）", "网页与接口传输的协议"),
        ("ORM", "对象关系映射（Object-Relational Mapping）", "把数据库行映射为对象"),
        ("UTC", "协调世界时（Coordinated Universal Time）", "世界统一时间基准"),
        ("CORS", "跨源资源共享（Cross-Origin Resource Sharing）", "浏览器跨来源访问控制"),
        ("CRUD", "增删改查（Create, Read, Update, Delete）", "数据的基本操作"),
        ("JSON", "JavaScript 对象表示法（JavaScript Object Notation）", "结构化数据文本格式"),
    ]
    doc.add_table(
        ("缩写 / Abbr.", "全称 / Full name", "中文含义 / Chinese meaning"),
        [[abbr, full, meaning] for abbr, full, meaning in glossary],
        widths=[3.0, 8.0, 5.5],
    )

    doc.add_pair(
        "恭喜完成 Phase 0 的学习。建议下一步：通读 backend/app/ 下的真实代码，再对照本文档用自己的话复述每个知识点。",
        "Congratulations on finishing Phase 0. Suggested next step: read the real code under backend/app/, "
        "then retell each knowledge point in your own words.",
    )
