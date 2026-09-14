"""Phase 1 学习文档内容：LLM Gateway 与结构化对话基础。

内容组织遵循项目文档规范（五段式知识点 / 代码讲解 / 英语学习增强），
中英段落严格逐段配对。所有代码示例来自 JARVIS 仓库真实文件。
"""

META = {
    "title_cn": "阶段 1：LLM Gateway 与对话系统",
    "title_en": "Phase 1: The LLM Gateway and the Conversation System",
    "subtitle_cn": "JARVIS 项目双语学习文档 · Provider 抽象 / 对话持久化 / 安全降级",
    "subtitle_en": "Bilingual learning material for the JARVIS project",
    "header": "JARVIS 双语学习 · 阶段 1 / Phase 1",
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
    # ---------------- 文档说明 ----------------
    doc.add_heading("文档说明", "About this document", level=1)
    doc.add_pair(
        "Phase 1 让 JARVIS 开始“说人话”：接入大语言模型（LLM，Large Language Model），实现对话的创建、持久化"
        "与模型调用。本阶段的核心工程思想是解耦——把「模型供应商」抽象成统一接口，让业务代码不依赖任何一家公司；"
        "以及安全边界——模型永远不能直接执行数据库副作用。",
        "Phase 1 lets JARVIS start speaking: it connects to a Large Language Model (LLM), and implements "
        "conversation creation, persistence, and model calls. The core engineering ideas are decoupling — "
        "abstracting the model vendor behind one interface so business code depends on no single company — "
        "and a safety boundary — the model can never execute database side effects directly.",
    )
    doc.add_pair(
        "建议先完成 Phase 0 文档的学习，再开始本阶段。英文段落紧跟对应的中文段落，代码只保留一份。",
        "Finish the Phase 0 document first. English paragraphs directly follow their Chinese partners, and "
        "code is shown only once.",
    )

    # ---------------- 架构 ----------------
    doc.add_heading("Phase 1 架构：消息如何走通全链路", "Phase 1 architecture: the message path", level=1)
    doc.add_architecture_table(
        "对话消息的完整数据流",
        "The full data flow of a conversation message",
        [
            (
                "客户端（前端）",
                "Client (frontend)",
                "POST /api/conversations/{id}/messages，内容为一条用户消息。",
                "POSTs a user message to /api/conversations/{id}/messages.",
            ),
            (
                "API 层（api/conversations.py）",
                "API layer",
                "用 Pydantic 校验请求体，把 Provider 作为依赖注入。",
                "Validates the payload with Pydantic and injects the provider as a dependency.",
            ),
            (
                "编排服务（services/chat_service.py）",
                "Orchestration service",
                "保存 USER 消息 → 组装完整历史 → 调用 Provider → 保存 ASSISTANT 消息。",
                "Saves the USER message, builds the full history, calls the provider, and saves the ASSISTANT message.",
            ),
            (
                "Provider（app/llm/）",
                "Provider (app/llm/)",
                "把历史消息发给模型，返回文本与 token、耗时等元数据。不碰数据库。",
                "Sends the history to the model and returns text plus metadata like tokens and latency. It never touches the database.",
            ),
        ],
    )
    doc.add_pair(
        "注意 Provider 位于调用链的末端且不做持久化——模型永远只负责「生成文本」，落库和副作用全部由编排服务控制。",
        "Notice the provider sits at the end of the chain and does no persistence — the model only generates "
        "text, while all persistence and side effects stay under the control of the orchestration service.",
    )

    # ---------------- 知识点 ----------------
    doc.add_heading("核心知识点", "Core knowledge points", level=1)

    _knowledge_point(
        doc,
        "LLM 基本概念：什么是大语言模型",
        "LLM basics: what a large language model is",
        "大语言模型（Large Language Model, LLM）是经过海量文本训练的程序，它根据你给它的消息序列，预测并生成下一段"
        "文本。它本身不“知道”你的数据库、你的文件或你的业务逻辑——它只做一件事：输入文本，输出文本。"
        "所以工程上必须由我们的代码来校验、持久化、控制副作用，而不是信任模型的输出。",
        "A Large Language Model (LLM) is a program trained on massive amounts of text. Given a sequence of "
        "messages, it predicts and generates the next text. It does not know your database, your files, or "
        "your business logic — it does one thing: text in, text out. So engineering-wise, our code must "
        "validate, persist, and control side effects; we never trust the model's output blindly.",
        [
            ("大语言模型 — LLM", "A model that generates text from messages"),
            ("提示词 — Prompt", "The messages we send to the model"),
            ("输出 — Output", "The generated text and metadata"),
            ("生成 — Generation", "The act of producing text"),
        ],
        "backend/app/llm/base.py 定义了 LLMProvider 抽象；chat_service.py 负责把模型输出持久化到 messages 表。",
        "backend/app/llm/base.py defines the LLMProvider abstraction, and chat_service.py persists the model "
        "output into the messages table.",
        (
            "“An LLM is a text-in, text-out system, so our service layer validates and persists everything before and after the call.”",
            "「大语言模型是文本进文本出的系统，所以我们的服务层在调用前后负责校验和持久化。」",
        ),
    )

    _knowledge_point(
        doc,
        "Conversation 与 Message：对话的两张表",
        "Conversation and Message: two tables for chat",
        "对话（Conversation）是一个会话的容器，有可选的标题；消息（Message）是会话里的一条记录，必须属于某个对话。"
        "消息有角色（role）：USER 是用户发的，ASSISTANT 是模型回的，SYSTEM 是系统提示，TOOL 是工具结果（本阶段未使用）。"
        "消息还记录调用元数据：模型名、输入 token 数、输出 token 数、耗时。",
        "A Conversation is a container for a chat session with an optional title; a Message is one record "
        "inside a conversation. Messages have a role: USER is from the user, ASSISTANT is the model reply, "
        "SYSTEM is a system prompt, and TOOL is a tool result (unused this phase). Messages also carry call "
        "metadata: model name, prompt tokens, completion tokens, and latency.",
        [
            ("会话 — Conversation", "A chat session container"),
            ("消息 — Message", "One record in a conversation"),
            ("角色 — Role", "Who produced the message: user, assistant, ..."),
            ("外键 — Foreign key", "Links a message to its conversation"),
        ],
        "backend/app/models/conversation.py 定义了 Conversation 与 Message；messages.conversation_id 是外键并带索引。",
        "backend/app/models/conversation.py defines Conversation and Message; messages.conversation_id is a "
        "foreign key with an index.",
        (
            "“A message belongs to exactly one conversation through a foreign key, and we index it for fast lookups.”",
            "「每条消息通过外键属于唯一一个对话，我们为它建了索引以便快速查询。」",
        ),
    )

    _knowledge_point(
        doc,
        "Provider 抽象：只依赖接口，不依赖供应商",
        "Provider abstraction: depend on the interface, not the vendor",
        "如果业务代码直接调用 OpenAI 的 SDK，将来换供应商就要改所有调用点。抽象（abstraction）就是定义一份统一"
        "接口（generate 方法），业务代码只认识这份接口，具体是哪家公司由工厂在启动时决定。这就是「面向接口编程」——"
        "依赖倒置，细节可以被替换。",
        "If business code called the OpenAI SDK directly, switching vendors would mean touching every call "
        "site. An abstraction defines one unified interface — the generate method — and business code only "
        "knows that interface; which company backs it is decided by a factory at startup. This is "
        "programming to an interface: dependencies are inverted, and implementations can be swapped.",
        [
            ("抽象 — Abstraction", "A shared interface hiding implementation details"),
            ("接口 — Interface", "The contract of methods an object must provide"),
            ("实现 — Implementation", "A concrete class that fulfills the interface"),
            ("可替换 — Swappable", "Able to be replaced without changing callers"),
        ],
        "backend/app/llm/base.py 的 LLMProvider 是抽象基类，OpenAIChatProvider 与 FakeLLMProvider 是两个实现。",
        "LLMProvider in backend/app/llm/base.py is the abstract base class; OpenAIChatProvider and "
        "FakeLLMProvider are the two implementations.",
        (
            "“Business code depends on the LLMProvider interface, so replacing the vendor never changes the service layer.”",
            "「业务代码只依赖 LLMProvider 接口，换供应商不用改服务层。」",
        ),
    )

    _knowledge_point(
        doc,
        "OpenAI Provider：第一个真实实现",
        "OpenAI Provider: the first real implementation",
        "OpenAIChatProvider 用 OpenAI 官方 SDK 调用 chat.completions.create。它把超时时间、模型名、API Key 作为"
        "构造参数传入，调用时用 time.monotonic() 测量耗时，把 SDK 的异常统一映射成我们自己的错误类型：超时映射为 "
        "LLMTimeoutError，其他错误映射为 LLMUpstreamError。SDK 的异常细节绝不外泄。",
        "OpenAIChatProvider uses the official OpenAI SDK to call chat.completions.create. Timeout, model "
        "name, and API key are constructor parameters; time.monotonic() measures latency; and SDK exceptions "
        "are mapped into our own error types — timeouts become LLMTimeoutError, other errors become "
        "LLMUpstreamError. SDK exception details never leak out.",
        [
            ("SDK — SDK", "The official library provided by the vendor"),
            ("超时 — Timeout", "The maximum time to wait for a response"),
            ("延迟 — Latency", "The measured time of the call"),
            ("异常映射 — Exception mapping", "Converting vendor errors into our own types"),
        ],
        "backend/app/llm/openai_provider.py 的 generate 方法实现全部映射逻辑；错误类型定义在 base.py。",
        "The generate method in backend/app/llm/openai_provider.py does all the mapping; error types live in base.py.",
        (
            "“I wrap the vendor SDK so that timeouts and API errors become our own exception types with a stable HTTP mapping.”",
            "「我封装了供应商 SDK，让超时和 API 错误变成我们自己的异常类型，并映射为稳定的 HTTP 响应。」",
        ),
    )

    _knowledge_point(
        doc,
        "Fake Provider：测试里绝不联网",
        "Fake Provider: tests never touch the network",
        "FakeLLMProvider 是一个本地模拟器：它返回固定的回复文本、模拟 token 与耗时，记录每次调用收到的消息列表，"
        "还能被配置成抛出错误或延迟。自动化测试必须用 Fake Provider——真实模型调用又慢、又花钱、结果还不确定，"
        "而且 CI 环境里根本没有网络或 API Key。",
        "FakeLLMProvider is a local simulator: it returns a fixed reply, simulates tokens and latency, "
        "records the messages it received on each call, and can be configured to raise errors or delay. "
        "Automated tests must use it — real model calls are slow, costly, and nondeterministic, and CI "
        "environments have no network or API key anyway.",
        [
            ("模拟器 — Simulator", "A fake object that mimics behavior locally"),
            ("确定性 — Determinism", "Same input always gives the same output"),
            ("记录调用 — Record calls", "Capturing inputs for assertions"),
            ("注入错误 — Inject errors", "Making the fake fail on demand"),
        ],
        "backend/app/llm/fake_provider.py 的 calls 列表保存每次调用的消息快照；tests/conftest.py 把依赖覆盖成 Fake。",
        "The calls list in backend/app/llm/fake_provider.py stores per-call message snapshots; tests/conftest.py "
        "overrides the dependency with the Fake provider.",
        (
            "“Tests inject a FakeLLMProvider through dependency overrides, so no test ever calls a real model.”",
            "「测试通过依赖覆盖注入 Fake Provider，任何测试都不会调用真实模型。」",
        ),
    )

    _knowledge_point(
        doc,
        "Factory：按配置组装 Provider",
        "Factory: assembling the provider from configuration",
        "工厂（factory）是一个决定「用哪个实现」的函数：它读环境变量 LLM_PROVIDER，返回对应的 Provider 实例。"
        "LLM_PROVIDER=openai 返回真实实现；=fake 返回模拟器；未知值抛配置错误。工厂把「选择」集中在一处，"
        "业务代码完全不用关心。",
        "A factory is a function that decides which implementation to use: it reads the LLM_PROVIDER "
        "environment variable and returns the matching provider instance. openai returns the real one; fake "
        "returns the simulator; unknown values raise a configuration error. The factory centralizes the "
        "choice, and business code never cares.",
        [
            ("工厂 — Factory", "A function that creates the right implementation"),
            ("环境变量 — Environment variable", "Configuration provided outside the code"),
            ("装配 — Assembly", "Building objects from configuration"),
            ("配置错误 — Configuration error", "Raised when the configuration is invalid"),
        ],
        "backend/app/llm/factory.py 的 get_llm_provider 就是工厂；它同时是 FastAPI 依赖，每个请求调用一次。",
        "get_llm_provider in backend/app/llm/factory.py is the factory; it is also a FastAPI dependency, "
        "resolved once per request.",
        (
            "“The factory reads an environment variable and returns the right provider, so switching vendors is a config change.”",
            "「工厂读取环境变量并返回对应的 Provider，换供应商只是一个配置改动。」",
        ),
    )

    _knowledge_point(
        doc,
        "依赖注入：谁需要谁声明",
        "Dependency injection: declare what you need",
        "依赖注入（Dependency Injection, DI）的意思是：一个函数需要的依赖（数据库会话、Provider），不由它自己创建，"
        "而是由框架在调用时提供。FastAPI 的 Depends() 就是注入机制。好处是：真实运行时注入真实现，测试时注入 Fake——"
        "同样的代码、不同的依赖，这就是可测试性的来源。",
        "Dependency Injection (DI) means a function's dependencies — the database session, the provider — are "
        "not created by the function itself but provided by the framework at call time. FastAPI's Depends() "
        "is the injection mechanism. The benefit: production injects real implementations, tests inject "
        "fakes — same code, different dependencies. That is where testability comes from.",
        [
            ("依赖 — Dependency", "Something a function needs from outside"),
            ("注入 — Injection", "Providing dependencies at call time"),
            ("覆盖 — Override", "Replacing a dependency for tests"),
            ("可测试性 — Testability", "How easily code can be tested"),
        ],
        "api/conversations.py 用 Depends(get_llm_provider) 声明依赖；conftest.py 用 app.dependency_overrides 换成 Fake。",
        "api/conversations.py declares Depends(get_llm_provider); conftest.py swaps it for the Fake via "
        "app.dependency_overrides.",
        (
            "“We use dependency injection, so tests simply override the provider dependency with a fake.”",
            "「我们用依赖注入，测试只需把 Provider 依赖覆盖成 Fake 即可。」",
        ),
    )

    _knowledge_point(
        doc,
        "对话上下文：把历史讲给模型听",
        "Conversation context: telling the model the history",
        "模型是无状态的：每一次调用它都“忘了”之前说过什么。所以每次发送消息时，服务层会从数据库读出这个对话的"
        "全部历史消息，按时间排序，组装成模型认识的格式（role + content），连同新消息一起发给模型。"
        "这就是多轮对话的实现原理——上下文由我们负责维护。",
        "The model is stateless: every call forgets what was said before. So on each message, the service "
        "reads the full history of the conversation from the database, sorts it by time, converts it to the "
        "model's format (role + content), and sends it together with the new message. That is how multi-turn "
        "chat works — maintaining context is our job.",
        [
            ("上下文 — Context", "The history the model sees"),
            ("无状态 — Stateless", "The model keeps no memory between calls"),
            ("历史消息 — History", "All previous messages of the conversation"),
            ("组装 — Assemble", "Converting records into provider messages"),
        ],
        "chat_service.send_message 中：读取历史 → 映射 role → 调用 provider.generate(provider_messages)。测试断言 Fake 收到的上下文包含全部历史。",
        "In chat_service.send_message: read the history, map the roles, then call "
        "provider.generate(provider_messages). Tests assert the Fake received the full history.",
        (
            "“The model is stateless, so the service rebuilds the full conversation context from the database on every message.”",
            "「模型是无状态的，所以服务层每次发消息都从数据库重建完整对话上下文。」",
        ),
    )

    _knowledge_point(
        doc,
        "Token usage：模型调用的成本计量",
        "Token usage: metering model calls",
        "模型按 token（词元，文本的基本计量单位）计费。输入 token 数叫 prompt_tokens，输出叫 completion_tokens。"
        "JARVIS 把这两个数字连同耗时（latency_ms）和模型名一起存进 ASSISTANT 消息，并在 API 响应里返回 usage 对象。"
        "这些数据对成本监控和性能分析非常有用。",
        "Models are billed per token, the basic unit of text. Input tokens are prompt_tokens and output "
        "tokens are completion_tokens. JARVIS stores both numbers, the latency in milliseconds, and the "
        "model name on the ASSISTANT message, and returns a usage object in the API response. This data is "
        "valuable for cost monitoring and performance analysis.",
        [
            ("词元 — Token", "The atomic text unit the model processes"),
            ("输入词元 — Prompt tokens", "Tokens sent to the model"),
            ("输出词元 — Completion tokens", "Tokens generated by the model"),
            ("元数据 — Metadata", "Data about the call: model, tokens, latency"),
        ],
        "Message 模型的 prompt_tokens/completion_tokens/latency_ms 字段；ChatExchangeOut 响应含 usage。",
        "The Message model has prompt_tokens, completion_tokens, and latency_ms fields; ChatExchangeOut "
        "includes a usage object.",
        (
            "“We store token usage and latency on every assistant message so we can monitor cost and performance.”",
            "「我们把 token 用量和耗时存在每条助手消息上，以便监控成本和性能。」",
        ),
    )

    _knowledge_point(
        doc,
        "Timeout：给外部调用一个截止线",
        "Timeout: a deadline for external calls",
        "外部 API 可能永远不返回。超时（timeout）就是给调用设置一个截止时间：LLM_TIMEOUT_SECONDS 秒内没返回就放弃，"
        "抛出 LLMTimeoutError，API 返回 502。没有超时的话，一个挂起的请求会一直占用服务器线程和用户耐心。"
        "JARVIS 不自动重试：生成型调用不保证幂等，重试可能重复计费或产生重复结果。",
        "External APIs may never return. A timeout sets a deadline: if the call does not finish within "
        "LLM_TIMEOUT_SECONDS, it is abandoned, LLMTimeoutError is raised, and the API returns 502. Without a "
        "timeout, a hung request would hold a server thread and the user's patience forever. JARVIS never "
        "auto-retries: generation is not idempotent, and retrying could double-charge or duplicate results.",
        [
            ("截止时间 — Deadline", "The latest moment the call may finish"),
            ("挂起 — Hung request", "A request that never completes"),
            ("放弃 — Abandon", "Giving up on a timed-out call"),
            ("重试 — Retry", "Calling again after a failure"),
        ],
        "config.py 的 llm_timeout_seconds 传入 OpenAIChatProvider；测试用 Fake 抛 LLMTimeoutError 验证 502 映射。",
        "llm_timeout_seconds in config.py is passed to OpenAIChatProvider; a test makes the Fake raise "
        "LLMTimeoutError to verify the 502 mapping.",
        (
            "“We enforce a timeout on every external call and deliberately do not auto-retry, because generation is not idempotent.”",
            "「每次外部调用都有超时限制，并且我们刻意不做自动重试，因为生成不保证幂等。」",
        ),
    )

    _knowledge_point(
        doc,
        "数据库一致性：失败时状态必须清楚",
        "Database consistency: failures must leave a clear state",
        "发送消息涉及两次写库（USER 和 ASSISTANT）和一次外部调用。如果模型调用失败，数据库绝不能处于“说不清”的"
        "状态。JARVIS 的策略是：USER 消息先落库提交，再调用模型；失败时只保留 USER 消息（用户确实说了话），"
        "不写半成品的 ASSISTANT；配置缺失时请求在编排前就被拒绝，什么都不写。两种失败都让 GET messages 一眼看清状态。",
        "Sending a message involves two writes (USER and ASSISTANT) and one external call. If the model call "
        "fails, the database must never be in an unclear state. JARVIS's strategy: the USER message is "
        "committed first, then the model is called; on failure only the USER message remains — the user "
        "really did say something — and no half-written ASSISTANT is stored; when configuration is missing, "
        "the request is rejected before orchestration starts and nothing is written. Both failures leave a "
        "state that GET messages makes obvious.",
        [
            ("一致性 — Consistency", "The database stays in a clear, explainable state"),
            ("半成品 — Half-written", "Partial data that is hard to explain"),
            ("先落库 — Commit first", "Persisting the user message before the model call"),
            ("可恢复 — Recoverable", "The conversation can continue after a failure"),
        ],
        "chat_service.send_message 的顺序与注释；test_failure_state_is_consistent_and_recoverable 验证失败后可继续对话。",
        "The ordering and comments in chat_service.send_message; test_failure_state_is_consistent_and_recoverable "
        "verifies the conversation stays usable after a failure.",
        (
            "“On provider failure we keep the user message and store no placeholder, so the database state is always explainable.”",
            "「Provider 失败时我们保留用户消息、不写占位内容，数据库状态永远解释得清。」",
        ),
    )

    _knowledge_point(
        doc,
        "无 API Key 时安全降级：其他功能照常工作",
        "Safe degradation without an API key: everything else keeps working",
        "API Key 是调用模型的凭证，绝不能写进代码仓库。开发机器上常常没有 Key。JARVIS 的设计是：没有 Key 时应用照常"
        "启动，任务管理、健康检查全部正常；只有发送消息接口返回 503 + LLM_NOT_CONFIGURED 的清晰错误。"
        "这让项目在任何环境都能启动和演示，也让错误信息可读而不是含糊的 500。",
        "An API key is the credential for calling the model, and it must never be committed to the "
        "repository. Dev machines often have no key. JARVIS's design: without a key the app starts normally, "
        "task management and health checks keep working; only the send-message endpoint returns 503 with the "
        "clear LLM_NOT_CONFIGURED error. This lets the project start and demo in any environment, and the "
        "error message is readable instead of a vague 500.",
        [
            ("凭证 — Credential", "A secret that authorizes access"),
            ("降级 — Degradation", "Failing gracefully for one feature only"),
            ("配置缺失 — Missing configuration", "A required setting is absent"),
            ("清晰错误 — Clear error", "A readable, specific error message"),
        ],
        "factory.py 无 Key 时抛 LLMNotConfiguredError；main.py 映射为 503；test_no_api_key_returns_clear_config_error 验证。",
        "factory.py raises LLMNotConfiguredError without a key; main.py maps it to 503; "
        "test_no_api_key_returns_clear_config_error verifies it.",
        (
            "“Without an API key the app still starts, health checks pass, and only chat returns a clear 503 configuration error.”",
            "「没有 API Key 时应用照常启动，健康检查通过，只有对话接口返回清晰的 503 配置错误。」",
        ),
    )

    # ---------------- 核心代码讲解 ----------------
    doc.add_heading("核心代码讲解", "Core code explained", level=1)

    doc.add_code_explain(
        {
            "location": "backend/app/llm/base.py — LLMProvider 抽象",
            "code": '''class LLMProvider(ABC):
    """模型供应商抽象。实现必须是无状态、可测试、不产生数据库副作用的。"""

    @abstractmethod
    def generate(
        self,
        messages: list[ProviderMessage],
        *,
        response_schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        """调用模型。

        messages: OpenAI 兼容的角色消息列表，由调用方构造完整上下文。
        response_schema: 为下一步 Structured Output 预留；本阶段实现可以忽略它。
        """
        raise NotImplementedError''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "这份抽象接口是所有 Provider 的契约：generate 接收消息列表，返回 LLMResponse（文本 + 元数据）。"
                    "response_schema 参数为下一步的 Structured Output 预留，本阶段实现可以忽略。",
                    "en": "This abstract interface is the contract for every provider: generate takes a message "
                    "list and returns an LLMResponse (text plus metadata). The response_schema parameter is "
                    "reserved for the upcoming Structured Output step; this phase's implementations may "
                    "ignore it.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("用 ABC 声明抽象基类，@abstractmethod 强制子类实现 generate。", "ABC declares the abstract base; @abstractmethod forces subclasses to implement generate."),
                        ("参数只有 messages 与 response_schema，没有数据库、没有 Key——契约保持纯粹。", "The only parameters are messages and response_schema — no database, no key — the contract stays pure."),
                        ("返回类型是 LLMResponse 数据类，包含文本、模型名、token 与耗时。", "The return type is the LLMResponse dataclass holding text, model name, tokens, and latency."),
                    ],
                    "en_intro": "The abstract base class forces subclasses to implement generate; the contract "
                    "takes only messages and an optional schema, and returns a dataclass of text plus metadata.",
                },
                {
                    "kind": "why",
                    "cn": "把接口收窄到一个方法、输入输出都结构化，任何供应商都能适配；业务代码写一次，供应商随便换。"
                    "无数据库参数意味着 Provider 天生无法产生副作用——安全边界从类型层面就保证了。",
                    "en": "A single narrow method with structured inputs and outputs lets any vendor adapt; "
                    "business code is written once and vendors can be swapped freely. With no database "
                    "parameter, a provider structurally cannot cause side effects — the safety boundary is "
                    "guaranteed at the type level.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是让 Provider 直接接收数据库会话或在内部偷偷调用业务服务，这会把「生成」和「执行」混在一起，"
                    "模型一旦输出恶意内容就可能触发副作用。另一个常见错误是接口里塞满某个供应商特有的参数，失去通用性。",
                    "en": "A common mistake is giving the provider a database session or letting it call "
                    "business services internally, mixing generation with execution — malicious model output "
                    "could then trigger side effects. Another is stuffing vendor-specific parameters into the "
                    "interface, destroying its generality.",
                },
            ],
        }
    )

    doc.add_code_explain(
        {
            "location": "backend/app/services/chat_service.py — send_message 编排",
            "code": '''    user_message = Message(
        conversation_id=conversation_id, role=MessageRole.USER.value, content=content
    )
    db.add(user_message)
    db.commit()
    db.refresh(user_message)

    history = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc(), Message.id.asc())
        .all()
    )
    provider_messages: list[ProviderMessage] = [
        {"role": _role_to_provider(MessageRole(m.role)), "content": m.content}
        for m in history
    ]

    started = time.monotonic()
    response = provider.generate(provider_messages)''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "这段代码是发送消息的核心编排：先把 USER 消息落库提交，再读取完整历史并组装成模型格式，最后调用"
                    "Provider。顺序不是随意的——先落库保证了失败时用户消息仍然存在。",
                    "en": "This is the core orchestration of sending a message: commit the USER message first, "
                    "then read the full history and assemble it into the model format, then call the provider. "
                    "The order is deliberate — committing first guarantees the user message survives a failure.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("创建 USER 消息并立即 commit——用户说了什么是确定的。", "Create and immediately commit the USER message — what the user said is now a fact."),
                        ("按时间与 id 升序读取该对话的全部历史。", "Read the conversation's full history ordered by time and id."),
                        ("把每条历史映射为 {role, content} 的模型格式。", "Map every history record into the model format {role, content}."),
                        ("用 time.monotonic() 计时后调用 provider.generate。", "Start the clock with time.monotonic() and call provider.generate."),
                    ],
                    "en_intro": "Commit the user message first; read the history in order; convert it to the "
                    "provider format; then generate with a monotonic timer.",
                },
                {
                    "kind": "why",
                    "cn": "先 commit 后调用是失败一致性策略：即使模型调用失败，用户消息已在库中，状态清晰可查，对话还能继续。"
                    "历史从数据库重建而不是内存缓存，重启服务也不丢上下文。",
                    "en": "Committing before the call is the failure-consistency strategy: even if the model "
                    "fails, the user message is stored, the state is clear, and the conversation can continue. "
                    "Rebuilding history from the database instead of caching in memory also survives restarts.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是先把模型调用放在前面、成功后才保存两条消息——失败时用户消息丢失，用户以为没发出去。"
                    "另一个常见错误是只发新消息不带上历史，模型每轮都失忆。",
                    "en": "A common mistake is calling the model first and saving both messages only on "
                    "success — a failure then loses the user message, and the user thinks nothing was sent. "
                    "Another is sending only the new message without history, so the model forgets every turn.",
                },
            ],
        }
    )

    doc.add_code_explain(
        {
            "location": "backend/app/llm/factory.py — get_llm_provider",
            "code": '''def get_llm_provider() -> LLMProvider:
    """FastAPI 依赖：每次请求按当前配置构造 Provider（无状态、可安全 override）。"""
    provider_name = settings.llm_provider.strip().lower()

    if provider_name == "fake":
        return FakeLLMProvider()

    if provider_name == "openai":
        if not settings.llm_api_key:
            raise LLMNotConfiguredError(
                "LLM_API_KEY is not configured (LLM_PROVIDER=openai). "
                "Set it in .env, or use LLM_PROVIDER=fake for local testing."
            )
        return OpenAIChatProvider(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            base_url=settings.llm_base_url.strip() or None,
            timeout_seconds=settings.llm_timeout_seconds,
        )

    raise LLMNotConfiguredError(
        f"Unknown LLM_PROVIDER {settings.llm_provider!r}. Supported: openai, fake"
    )''',
            "sections": [
                {
                    "kind": "purpose",
                    "cn": "工厂按环境变量决定返回哪个 Provider。fake 直接返回模拟器；openai 需要 Key，没有 Key 就抛配置错误；"
                    "未知值也抛配置错误。错误消息清晰说明了如何修复。",
                    "en": "The factory decides which provider to return based on environment variables. fake "
                    "returns the simulator directly; openai requires a key and raises a configuration error "
                    "without one; unknown values raise as well. The messages explain how to fix the problem.",
                },
                {
                    "kind": "steps",
                    "steps": [
                        ("读取并规范化 LLM_PROVIDER 的值。", "Read and normalize the LLM_PROVIDER value."),
                        ("fake 分支返回 FakeLLMProvider，零配置。", "The fake branch returns FakeLLMProvider with zero configuration."),
                        ("openai 分支先检查 Key，缺失则抛 LLMNotConfiguredError。", "The openai branch checks the key first and raises LLMNotConfiguredError when absent."),
                        ("构造真实 Provider，把超时、模型、Base URL 全部注入。", "Construct the real provider, injecting timeout, model, and base URL."),
                    ],
                    "en_intro": "Normalize the provider name; return the fake for zero config; for openai, "
                    "check the key, raise a clear error if missing, and otherwise build the provider with "
                    "all settings injected.",
                },
                {
                    "kind": "why",
                    "cn": "Key 从环境变量读取，代码里没有密钥；无 Key 时抛出的异常由 API 层映射为 503，而不是让应用崩溃。"
                    "工厂同时作为 FastAPI 依赖，测试可以通过依赖覆盖彻底替换它。",
                    "en": "The key comes from an environment variable, so no secret lives in code; without a "
                    "key the raised error maps to a 503 at the API layer instead of crashing the app. Being "
                    "a FastAPI dependency also means tests can override the factory entirely.",
                },
                {
                    "kind": "mistake",
                    "cn": "常见错误是把 API Key 写进代码或配置文件并提交到仓库；另一个常见错误是启动时就解析 Key 并崩溃——"
                    "应该延迟到真正调用模型时才报错，让其他功能不受影响。",
                    "en": "A common mistake is hard-coding the API key into code or config files and "
                    "committing it; another is parsing the key at startup and crashing — the error should "
                    "only appear when a model call actually happens, leaving other features untouched.",
                },
            ],
        }
    )

    # ---------------- 技术英语 ----------------
    doc.add_heading("Technical English 技术英语", "Technical English vocabulary", level=1)
    doc.add_pair(
        "以下 22 个核心词汇覆盖 Phase 1 的关键概念。",
        "The 22 core terms below cover the key concepts of Phase 1.",
    )
    vocab = [
        ("Large Language Model", "大语言模型", "A text-generation model",
         "A model trained on massive text that generates text from messages.",
         "The OpenAI-compatible model behind JARVIS chat.",
         "call an LLM, prompt an LLM"),
        ("Provider", "供应商/提供者", "The abstraction behind a vendor",
         "An abstraction that generates text; the vendor-specific part is hidden.",
         "LLMProvider is the interface; OpenAI and Fake are implementations.",
         "abstract the provider, swap the provider"),
        ("Abstraction", "抽象", "An interface hiding details",
         "A shared interface that hides implementation details.",
         "Business code depends on LLMProvider, not on OpenAI.",
         "program to an abstraction, hide details"),
        ("Factory", "工厂", "Creates implementations from config",
         "A function that creates the right implementation from configuration.",
         "get_llm_provider() returns Fake or OpenAI based on LLM_PROVIDER.",
         "use a factory, build via factory"),
        ("Dependency injection", "依赖注入", "Dependencies provided from outside",
         "A dependency is provided from outside instead of being created internally.",
         "The FakeLLMProvider is injected during tests.",
         "inject a dependency, replace a dependency, dependency override"),
        ("Dependency override", "依赖覆盖", "Replacing a dependency in tests",
         "Replacing a dependency with a substitute for testing.",
         "conftest.py overrides get_llm_provider with the Fake.",
         "override the dependency, restore the override"),
        ("Conversation", "对话/会话", "A chat session container",
         "A container for a chat session with its messages.",
         "The conversations table stores one row per chat.",
         "create a conversation, select a conversation"),
        ("Message", "消息", "One record in a chat",
         "One record in a conversation with a role and content.",
         "The messages table stores USER and ASSISTANT rows.",
         "persist a message, list messages"),
        ("Role", "角色", "Who produced the message",
         "Who produced the message: USER, ASSISTANT, SYSTEM, or TOOL.",
         "MessageRole.USER is stored for every user message.",
         "assign a role, map the role"),
        ("Foreign key", "外键", "A link to another table's row",
         "A column that links a row to another table's row.",
         "messages.conversation_id references conversations.id with CASCADE.",
         "define a foreign key, cascade on delete"),
        ("Context", "上下文", "The history sent to the model",
         "The history sent to the model so it can answer coherently.",
         "The service rebuilds the full history from the database.",
         "build the context, pass the context"),
        ("Stateless", "无状态", "No memory between calls",
         "The model keeps no memory between calls.",
         "Every generate() call receives the full history again.",
         "stateless model, rebuild state"),
        ("Token", "词元", "The atomic text unit",
         "The atomic text unit a model processes and bills by.",
         "prompt_tokens and completion_tokens are stored per message.",
         "count tokens, track usage"),
        ("Prompt tokens", "输入词元", "Input tokens",
         "Tokens sent to the model as input.",
         "The Fake reports prompt_tokens equal to the words in context.",
         "measure prompt tokens"),
        ("Completion tokens", "输出词元", "Output tokens",
         "Tokens generated by the model as output.",
         "Stored on the ASSISTANT message for cost analysis.",
         "count completion tokens"),
        ("Latency", "延迟", "Time a call takes",
         "The time a call takes, measured in milliseconds.",
         "time.monotonic() measures each generate() call.",
         "measure latency, reduce latency"),
        ("Timeout", "超时", "A deadline for a call",
         "A deadline after which a call is abandoned.",
         "LLM_TIMEOUT_SECONDS is passed to the OpenAI client.",
         "enforce a timeout, raise on timeout"),
        ("Upstream error", "上游错误", "An error from the external service",
         "An error returned by the external service we call.",
         "SDK errors map to LLMUpstreamError and then to HTTP 502.",
         "map upstream errors, report upstream failure"),
        ("Retry", "重试", "Calling again after failure",
         "Calling an operation again after it failed.",
         "JARVIS never auto-retries because generation is not idempotent.",
         "retry the call, avoid retries"),
        ("Consistency", "一致性", "A clear, explainable state",
         "The database remains in a clear, explainable state.",
         "The USER message is committed before the model call.",
         "ensure consistency, explainable state"),
        ("Side effect", "副作用", "An external change caused by an action",
         "An external change caused by an operation.",
         "Providers take no database, so they structurally cannot write.",
         "avoid side effects, control side effects"),
        ("Safe degradation", "安全降级", "Failing softly for one feature",
         "A missing feature fails gracefully while the rest keeps working.",
         "Without a key, chat returns 503 but tasks and health work.",
         "degrade gracefully, fail soft"),
    ]
    doc.add_table(
        ("术语 / Term", "含义 / Meaning", "定义 / Simple definition", "JARVIS 实例 / Example", "常见搭配 / Phrases"),
        [[cn, en_meaning, definition, example, phrases] for term, cn, en_meaning, definition, example, phrases in vocab],
        widths=[2.9, 2.1, 4.6, 3.4, 3.0],
    )

    # ---------------- Reading Code Aloud ----------------
    doc.add_heading("Reading Code Aloud 代码口头表达", "How to describe code in English", level=1)
    reading = [
        (
            "抽象接口（llm/base.py）",
            "“The abstract provider exposes a single generate method, so the orchestration layer never knows which vendor is behind the call.”",
            "「抽象 Provider 只暴露一个 generate 方法，编排层永远不知道调用背后是哪家供应商。」",
            "接口收窄到只有一个方法，输入输出都是结构化数据。",
            "The interface is narrowed to one method with structured inputs and outputs.",
        ),
        (
            "工厂（llm/factory.py）",
            "“The factory reads the provider name from the environment and returns the matching implementation, or raises a clear configuration error.”",
            "「工厂从环境变量读取供应商名，返回对应的实现，或者抛出清晰的配置错误。」",
            "无 Key 时抛出的错误由 API 层映射为 503。",
            "The missing-key error is mapped to a 503 by the API layer.",
        ),
        (
            "对话编排（services/chat_service.py）",
            "“The service commits the user message first, rebuilds the full history from the database, and only then calls the model.”",
            "「服务先提交用户消息，从数据库重建完整历史，然后才调用模型。」",
            "这个顺序保证了失败时数据库状态依然清晰。",
            "This ordering keeps the database state explainable on failure.",
        ),
        (
            "Fake Provider（llm/fake_provider.py）",
            "“The fake provider records every call it receives and can be configured to raise errors, so tests cover failure paths without the network.”",
            "「Fake Provider 记录每次收到的调用，还能配置成抛出错误，测试就能在无网络的情况下覆盖失败路径。」",
            "依赖覆盖让测试替换掉真实工厂。",
            "Dependency overrides replace the real factory during tests.",
        ),
        (
            "错误映射（main.py）",
            "“The handler maps configuration problems to 503 and upstream failures to 502, keeping the message stable so nothing internal leaks.”",
            "「处理器把配置问题映射为 503、上游失败映射为 502，消息保持稳定，内部细节不外泄。」",
            "客户端永远看不到原始异常或供应商响应。",
            "Clients never see the raw exception or the vendor response.",
        ),
    ]
    for title, sentence, meaning, note_cn, note_en in reading:
        doc.add_heading(title, title, level=3)
        doc.add_pair(sentence, meaning)
        doc.add_label("延伸说明 / Extended note")
        doc.add_pair(note_cn, note_en)

    # ---------------- Interview English ----------------
    doc.add_heading("Interview English 面试英语", "Interview practice", level=1)
    interviews = [
        (
            "为什么要把模型供应商抽象成接口？",
            "Why abstract the model vendor behind an interface?",
            "为了可替换与可测试。业务代码只依赖 LLMProvider 接口，换供应商只改工厂配置；测试用 Fake Provider 覆盖依赖，"
            "不联网不花钱。",
            "For swappability and testability. Business code only depends on the LLMProvider interface, so "
            "switching vendors is a factory config change; tests override the dependency with a Fake "
            "provider, needing neither network nor money.",
            "program to an interface, dependency override",
        ),
        (
            "模型调用失败时，数据库状态如何处理？",
            "How do you handle database state when the model call fails?",
            "USER 消息先提交。模型失败时只保留 USER 消息，不写半成品；配置缺失时请求在编排前被拒绝。GET messages "
            "永远能反映真实状态。",
            "The USER message is committed first. On model failure only the USER message remains — no "
            "half-written ASSISTANT; on missing configuration the request is rejected before orchestration. "
            "GET messages always reflects the real state.",
            "commit first, no half-written rows",
        ),
        (
            "为什么不做自动重试？",
            "Why no automatic retries?",
            "生成型调用不保证幂等：重试可能重复计费或产生不同结果。如果将来做重试，必须证明请求幂等，或只在读取类"
            "调用上重试。",
            "Generation is not guaranteed to be idempotent: a retry could double-charge or produce a "
            "different result. If we ever add retries, we must prove idempotency or retry only read-like calls.",
            "idempotent, double-charge",
        ),
        (
            "测试如何确保不调用真实模型？",
            "How do your tests guarantee no real model is called?",
            "夹具把 get_llm_provider 依赖覆盖为 FakeLLMProvider。Fake 完全本地运行，返回固定回复并记录调用，"
            "还能注入超时和错误来测失败路径。",
            "The fixture overrides the get_llm_provider dependency with FakeLLMProvider. The Fake runs fully "
            "locally, returns a fixed reply, records calls, and can inject timeouts and errors to exercise "
            "failure paths.",
            "fixture override, inject errors",
        ),
        (
            "对话上下文是怎么维护的？",
            "How is the conversation context maintained?",
            "模型无状态，所以服务层每次从数据库读取完整历史，按时间排序，组装成 role 加 content 的格式连同新消息"
            "一起发送。上下文由我们持久化，重启也不丢。",
            "The model is stateless, so the service reads the full history from the database every time, "
            "sorts it by time, assembles role-plus-content messages, and sends them with the new message. "
            "Context persists in our database and survives restarts.",
            "rebuild the context, persist history",
        ),
        (
            "token 用量数据有什么用？",
            "What are token usage numbers used for?",
            "用于成本监控与性能分析。每条 ASSISTANT 消息都存了 prompt_tokens、completion_tokens 和 latency_ms，"
            "API 响应里也有 usage 对象，前端或监控系统可以直接展示。",
            "For cost monitoring and performance analysis. Every ASSISTANT message stores prompt_tokens, "
            "completion_tokens, and latency_ms, and the API response includes a usage object, so dashboards "
            "can display them directly.",
            "cost monitoring, track usage",
        ),
        (
            "无 API Key 时你的应用会怎样？",
            "What happens to your app without an API key?",
            "应用正常启动，任务和健康检查照常工作；只有发送消息返回 503 和 LLM_NOT_CONFIGURED 清晰错误。"
            "Key 从环境变量读取，永远不会提交到仓库。",
            "The app starts normally; tasks and health checks keep working; only sending a message returns "
            "503 with the clear LLM_NOT_CONFIGURED error. The key comes from an environment variable and is "
            "never committed to the repository.",
            "degrade gracefully, environment variable",
        ),
        (
            "超时为什么重要？",
            "Why does a timeout matter?",
            "外部 API 可能挂起。没有超时，一个请求会一直占用线程和资源。我们用 LLM_TIMEOUT_SECONDS 设置截止时间，"
            "超时抛 LLMTimeoutError 并映射为 502。",
            "External APIs can hang. Without a timeout, one request would hold a thread forever. We set a "
            "deadline with LLM_TIMEOUT_SECONDS; a timeout raises LLMTimeoutError, mapped to a 502.",
            "set a deadline, hold a thread",
        ),
        (
            "模型如何被阻止执行副作用？",
            "How is the model prevented from causing side effects?",
            "在类型层面：Provider 接口根本没有数据库参数，它只能返回文本和元数据。持久化由编排服务控制，"
            "任何模型输出都先经过我们的 schema 校验。",
            "At the type level: the provider interface has no database parameter — it can only return text "
            "and metadata. Persistence is controlled by the orchestration service, and every model output "
            "passes through our schemas first.",
            "type-level guarantee, orchestration control",
        ),
        (
            "如何向团队证明你的 LLM 集成是可测试的？",
            "How do you prove your LLM integration is testable?",
            "54 个测试全部通过，其中 22 个是 Phase 1 新增：覆盖正常对话、失败一致性、超时、上游错误、无 Key 降级，"
            "全部使用 Fake Provider，测试期间零外部请求。",
            "All 54 tests pass, 22 of them new in Phase 1: covering normal chat, failure consistency, "
            "timeouts, upstream errors, and no-key degradation — all with the Fake provider and zero "
            "external requests during tests.",
            "test coverage, zero external calls",
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
    doc.add_heading("Sixty-second Introduction 一分钟项目介绍", "Introducing the LLM gateway in one minute", level=1)
    intro_cn = (
        "在 Phase 1，我给 JARVIS 接入了一个大语言模型网关。核心设计是供应商解耦：业务代码只依赖一个统一的 Provider "
        "接口，OpenAI 兼容实现和 Fake 实现通过环境变量切换。对话和消息持久化在 SQLite 里，每次发消息，服务层先从"
        "数据库重建完整上下文，再调用模型，把 token 用量和耗时连同回复一起保存。失败处理有三层：超时映射为 502，"
        "上游错误映射为 502，没有 API Key 时返回清晰的 503，应用其他功能完全不受影响。所有测试用 Fake Provider，"
        "54 个测试全部通过，期间零外部调用。"
    )
    intro_en = (
        "In Phase 1 I added an LLM gateway to JARVIS. The core design is vendor decoupling: business code "
        "depends on a single provider interface, and the OpenAI-compatible implementation and the Fake "
        "implementation are switched via environment variables. Conversations and messages persist in "
        "SQLite; on every message the service rebuilds the full context from the database, calls the model, "
        "and stores token usage and latency together with the reply. Failure handling has three layers: "
        "timeouts map to 502, upstream errors map to 502, and a missing API key returns a clear 503 while "
        "the rest of the app keeps working. All tests use the Fake provider — 54 tests pass with zero "
        "external calls."
    )
    doc.add_label("中文版本")
    doc.add_cn_only(intro_cn)
    doc.add_label("English version")
    doc.add_en_only(intro_en)
    doc.add_pair(
        "断句提示：五句——① 网关是什么；② 解耦设计；③ 上下文与元数据；④ 三层失败处理；⑤ 测试证据。"
        "强调词：vendor decoupling、provider interface、rebuilds the full context、502、503、54 tests、zero external calls。",
        "Pacing: five sentences — ① what the gateway is; ② the decoupling design; ③ context and metadata; "
        "④ the three failure layers; ⑤ the testing evidence. Stress: vendor decoupling, provider "
        "interface, rebuilds the full context, 502, 503, 54 tests, zero external calls.",
    )

    # ---------------- Writing Practice ----------------
    doc.add_heading("Writing Practice 写作练习", "Writing practice", level=1)
    writing = [
        "Explain in English why the LLMProvider interface takes no database parameter, and why that matters for safety.",
        "Explain in English what the FakeLLMProvider is and how dependency overrides make tests deterministic.",
        "Write an API error report in English: a chat message returns 502 LLM_TIMEOUT. Describe the request, the response, and what the server does with the database.",
        "Explain in English how the conversation context is rebuilt from the database on every message, and why the model cannot remember by itself.",
        "Explain in English how the app degrades safely when LLM_API_KEY is missing, and why the key must not be committed to the repository.",
    ]
    for i, q in enumerate(writing, start=1):
        doc.add_heading(f"写作题 {i}", f"Writing task {i}", level=3)
        doc.add_en_only(q)

    doc.add_heading("写作练习参考答案", "Writing practice reference answers", level=1)
    answers = [
        (
            "The provider interface takes no database parameter because its only job is to generate text. "
            "Without a database handle, a provider structurally cannot write or mutate anything — the "
            "safety boundary holds at the type level. Persistence stays under the orchestration service, "
            "which controls exactly when and how rows are written.",
            "Provider 接口不接收数据库参数，因为它的唯一职责是生成文本。没有数据库句柄，Provider 在结构上就无法写入"
            "或修改任何东西——安全边界在类型层面就成立了。持久化保留在编排服务里，由它精确控制何时、如何写行。",
        ),
        (
            "FakeLLMProvider is a local simulator: it returns a fixed reply, records the messages it "
            "receives, and can be configured to raise errors or sleep. Dependency overrides replace the "
            "real factory in tests, so the same code path runs against the fake. This makes tests fast, "
            "free, and deterministic.",
            "Fake Provider 是本地模拟器：返回固定回复、记录收到的消息，还能配置成抛错或休眠。依赖覆盖在测试中替换"
            "真实工厂，同一段代码路径对着 Fake 运行。这让测试快速、免费且确定。",
        ),
        (
            "Request: POST /api/conversations/1/messages. Response: HTTP 502 with "
            "{\"error\": {\"code\": \"LLM_TIMEOUT\", \"message\": \"LLM provider timed out\"}}. Server-side, the "
            "USER message was already committed before the call, so the database holds one USER message "
            "and no placeholder. The client can retry the message manually; the server never auto-retries.",
            "请求：POST /api/conversations/1/messages。响应：HTTP 502 加统一错误结构。服务端在调用前已提交 USER"
            "消息，所以数据库里只有一条 USER 消息，没有占位内容。客户端可以手动重发；服务端绝不自动重试。",
        ),
        (
            "The model is stateless: each call starts with a blank memory. The service therefore reads all "
            "messages of the conversation from the database, sorts them by creation time, maps each to "
            "role plus content, and sends the whole history with the new message. Because the history "
            "lives in our database, context survives restarts and multiple clients.",
            "模型是无状态的：每次调用都从空白记忆开始。因此服务层从数据库读取对话的全部消息，按创建时间排序，"
            "把每条映射为 role 加 content，连同新消息一起发送整个历史。因为历史存在我们的数据库里，上下文能跨重启、"
            "跨客户端存活。",
        ),
        (
            "Without LLM_API_KEY the app starts normally: tasks, health checks, and conversation creation "
            "all work. Sending a message returns 503 LLM_NOT_CONFIGURED with a readable message. The key "
            "must live in an environment variable, never in the repository, because committing a secret "
            "exposes it to everyone with repo access and it cannot be truly un-leaked.",
            "没有 LLM_API_KEY 时应用照常启动：任务、健康检查、创建对话都正常。发送消息返回 503 LLM_NOT_CONFIGURED"
            "与可读信息。Key 必须放在环境变量里，绝不能提交进仓库——提交密钥等于把它暴露给所有有仓库访问权的人，"
            "而且一旦泄露就无法真正收回。",
        ),
    ]
    for i, (en, cn) in enumerate(answers, start=1):
        doc.add_heading(f"参考答案 {i}", f"Reference answer {i}", level=3)
        doc.add_en_only(en)
        doc.add_cn_only(cn)

    # ---------------- Speaking Practice ----------------
    doc.add_heading("Speaking Practice 口语练习", "Speaking practice", level=1)
    speaking = [
        ("一句话版：用一句英文说出 Provider 抽象的核心作用。",
         "One-sentence version: state the core purpose of the provider abstraction in one English sentence.",
         "The provider abstraction lets business code call any LLM vendor through one interface, so switching vendors never changes the service layer.",
         "Provider 抽象让业务代码通过一个接口调用任何大模型供应商，换供应商不用改服务层。"),
        ("一句话版：用一句英文解释测试为什么必须用 Fake。",
         "One-sentence version: explain in one sentence why tests must use the Fake provider.",
         "Tests must use the Fake provider because real model calls are slow, costly, and nondeterministic.",
         "测试必须用 Fake Provider，因为真实模型调用又慢、又花钱、结果还不确定。"),
        ("30 秒版：描述发送一条消息时数据库和模型之间发生了什么。",
         "30-second version: describe what happens between the database and the model when a message is sent.",
         "First the user message is committed. Then the full history is read and converted to the model format. The provider generates a reply, and the assistant message is committed with its metadata.",
         "先提交用户消息。然后读取完整历史并转换成模型格式。Provider 生成回复，助手消息连同元数据一起提交。"),
        ("30 秒版：解释超时与不重试的决定。",
         "30-second version: explain the timeout and no-retry decisions.",
         "Every external call has a timeout mapped to a 502 response. We do not auto-retry because generation is not idempotent — a retry could double-charge or change the result.",
         "每次外部调用都有超时并映射为 502。我们不自动重试，因为生成不保证幂等——重试可能重复计费或改变结果。"),
        ("一分钟版：向同事解释为什么没有 API Key 时系统仍然能用。",
         "One-minute version: explain to a colleague why the system still works without an API key.",
         "The key is read from an environment variable at request time, not at startup. Without it, only the send-message dependency raises a configuration error, which becomes a 503. Task management, health checks, and conversation storage all work, because they never depend on the provider. This is safe degradation: the feature that needs the missing secret fails loudly and clearly, while everything else keeps running.",
         "Key 在请求时从环境变量读取，而不是启动时。没有它，只有发送消息的依赖会抛配置错误并变成 503。任务管理、"
         "健康检查、对话存储全部正常，因为它们根本不依赖 Provider。这就是安全降级：需要缺失凭据的功能清晰响亮地"
         "失败，其他一切照常运行。"),
    ]
    for i, (cn, en, answer, meaning) in enumerate(speaking, start=1):
        doc.add_heading(f"口语练习 {i}", f"Speaking practice {i}", level=3)
        doc.add_pair(cn, en)
        doc.add_label("示范回答")
        doc.add_en_only(answer)
        doc.add_cn_only(meaning)

    # ---------------- 术语表 ----------------
    doc.add_heading("术语速查表", "Quick glossary", level=1)
    glossary = [
        ("LLM", "大语言模型（Large Language Model）", "海量文本训练、文本进文本出的模型"),
        ("SDK", "软件开发工具包（Software Development Kit）", "供应商提供的官方调用库"),
        ("API", "应用程序编程接口（Application Programming Interface）", "程序之间对话的约定"),
        ("DI", "依赖注入（Dependency Injection）", "依赖由外部提供而非内部创建"),
        ("DB", "数据库（Database）", "结构化数据的持久化存储"),
        ("ORM", "对象关系映射（Object-Relational Mapping）", "把数据库行映射为对象"),
        ("UTC", "协调世界时（Coordinated Universal Time）", "世界统一时间基准"),
    ]
    doc.add_table(
        ("缩写 / Abbr.", "全称 / Full name", "中文含义 / Chinese meaning"),
        [[abbr, full, meaning] for abbr, full, meaning in glossary],
        widths=[3.0, 8.0, 5.5],
    )

    doc.add_pair(
        "Phase 1 学习完成。下一步建议：对照 backend/app/llm/ 与 services/chat_service.py 的真实代码，"
        "用英文向自己复述一遍「发送消息」的完整链路。",
        "Phase 1 complete. Next step: read the real code under backend/app/llm/ and "
        "services/chat_service.py, then retell the full send-message chain to yourself in English.",
    )
