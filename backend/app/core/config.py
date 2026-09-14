"""应用配置。所有值均可通过环境变量或根目录 .env 覆盖。"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(PROJECT_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "JARVIS Task API"
    database_url: str = "sqlite:///./jarvis.db"
    # 逗号分隔的 CORS 来源列表（环境变量无法直接传 JSON，因此用字符串）
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Phase 9A: Windows local public client. No client secret is supported.
    jarvis_microsoft_client_id: str = ""
    jarvis_microsoft_authority: str = "https://login.microsoftonline.com/common"
    jarvis_microsoft_graph_base: str = "https://graph.microsoft.com/v1.0"
    jarvis_microsoft_redirect_uri: str = "http://localhost:8000/api/calendar/oauth/callback"

    # ---- LLM Gateway ----
    # 支持: openai（OpenAI 兼容 API）/ deepseek（DeepSeek 官方 API）/
    #       fake（本地模拟，无需 Key，用于演示与测试）
    llm_provider: str = "openai"
    # 不设置默认 Key。留空时应用正常启动，仅消息生成接口返回 LLM_NOT_CONFIGURED
    llm_api_key: str = ""
    # 默认值是 OpenAI 的 gpt-4o-mini；LLM_PROVIDER=deepseek 时请自行设置为
    # deepseek-chat（或 deepseek-reasoner），本项目不会把 DeepSeek 绑定到该默认值
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 30.0
    # 可选：OpenAI 兼容端点（自托管网关 / 其他供应商）。留空时：
    # openai 使用官方端点，deepseek 安全默认 https://api.deepseek.com
    llm_base_url: str = ""
    # 仅 LLM_PROVIDER=fake 时生效：把 fake 的回复固定为 JSON（本地演示完整流程用）
    llm_fake_reply_json: str = ""

    # ---- Reminder Worker ----
    # 非法值（非整数/越界）由 Field 约束给出清晰的配置错误（应用启动即失败）
    reminder_poll_interval_seconds: int = Field(default=5, ge=1, le=3600)
    reminder_batch_size: int = Field(default=50, ge=1, le=1000)
    reminder_lease_seconds: int = Field(default=60, ge=1, le=3600)
    reminder_max_attempts: int = Field(default=5, ge=1, le=100)

    # ---- Document Ingestion（Phase 4A） ----
    # 上传文件存储目录。默认项目根目录 data/documents，可按需覆盖。
    # 该目录只接受服务端生成的 UUID 存储键（无扩展名），不接受任意路径。
    document_storage_dir: str = str(PROJECT_ROOT / "data" / "documents")
    # 单个上传文件大小上限（MB）。非法值由 Field 约束给出清晰配置错误
    document_max_file_size_mb: int = Field(default=10, ge=1, le=50)
    # 确定性切块参数（字符数）：目标块大小与相邻块之间的固定重叠。
    # 仅用于生成稳定可复现的块序列，不依赖任何 LLM。
    document_chunk_size_chars: int = Field(default=1200, ge=200, le=10000)
    document_chunk_overlap_chars: int = Field(default=200, ge=0, le=1000)

    # ---- Lightweight Retrieval（Phase 4B + 5B） ----
    # 嵌入 provider：local-hash（默认，零网络词汇向量）/
    # fake（固定向量，仅测试用，绝不生产静默启用）/ openai（真实语义 Embedding，
    # 需显式配置 EMBEDDING_MODEL；普通测试全部 mock，真实联网由
    # RUN_LIVE_EMBEDDING_TESTS=1 单独门控）。
    # 未知值必须在启动时失败，绝不静默降级（validator 兜底）。
    embedding_provider: str = "local-hash"
    # 外部 provider 的模型名（local-hash / fake 忽略）。openai 必填，否则启动失败。
    # 维度契约：向量语义随模型变化，更换模型/维度后必须显式 reindex（见 README）。
    embedding_model: str = ""
    # LocalHash 向量维度（单一配置来源；未来换真实 provider 时由 provider 决定）。
    # PostgreSQL 的 pg_chunk_vectors.embedding 是固定 VECTOR(dimension) 列：
    # 已建表后仅改本配置而 schema 未变 → 运行时稳定报错 VECTOR_DIMENSION_MISMATCH，
    # 绝不自动截断/填充/转换。
    embedding_dimension: int = Field(default=384, ge=64, le=4096)
    # 批量/单文本上限：防内存滥用（chunk 最长约 chunk_size+overlap 字符，正常远低于上限）
    embedding_batch_max: int = Field(default=256, ge=1, le=10000)
    embedding_max_text_chars: int = Field(default=20000, ge=100, le=100000)
    # 外部 embedding API 超时（秒）与 Key（留空时 openai provider 构造即失败，
    # 绝不允许静默回退到 local-hash；Key 只经 pydantic-settings 从 .env 读取）
    embedding_timeout_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    embedding_api_key: str = ""
    # 可选：OpenAI 兼容 Embedding 端点 base URL（自托管网关 / 兼容供应商）。
    # 留空 → 官方端点。不假设任何非 OpenAI 供应商（如 DeepSeek）一定提供
    # embedding 端点：运行期以实际响应验证为准（数量/顺序/维度/有限数值）。
    embedding_base_url: str = ""
    # 外部 embedding 请求的有限重试（仅明确可重试错误：429 / 部分 5xx /
    # timeout；401/403、非法响应、维度错误绝不重试）。指数退避的基数。
    embedding_max_retries: int = Field(default=2, ge=0, le=5)
    embedding_retry_backoff_seconds: float = Field(default=0.5, ge=0.0, le=10.0)
    # Hybrid Search：RRF 融合参数。keyword/vector 权重与 rrf_k 由受控配置提供；
    # final_score = kw/(k+rank_kw) + vec/(k+rank_vec)，两分数量纲不同，绝不直接相加。
    retrieval_keyword_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    retrieval_vector_weight: float = Field(default=0.5, ge=0.0, le=1.0)
    retrieval_rrf_k: float = Field(default=60.0, ge=1.0, le=1000.0)
    # 向量全扫描候选上限：当前规模允许 Python 全扫描；超限报错并提示未来迁移 pgvector。
    # 不声称具备大规模生产性能。
    retrieval_candidate_limit: int = Field(default=10000, ge=100, le=100000)

    # ---- Grounded RAG（Phase 4C） ----
    # 证据上下文总字符预算：超出时从尾部丢弃整块（不切块截断，引用完整性不受影响）。
    rag_max_context_chars: int = Field(default=30000, ge=1000, le=500000)
    # 引用 quote 的最大字符数：从数据库 chunk 内容确定性截取真实连续子串（前缀）。
    rag_quote_max_chars: int = Field(default=600, ge=50, le=10000)
    # 向量侧最小命中分数闸门：RRF 融合分是秩次函数（与相似度绝对值无关），
    # 不能直接阈值化；因此 vector/hybrid 模式下，纯向量命中的块余弦低于该值
    # 视为弱命中并丢弃（keyword 命中是 FTS 真实匹配，天然保留）。
    # 注意：LocalHash 在 384 维下不相干文本也有 ~0.1-0.3 的碰撞噪声余弦，
    # 该值按测试语料噪声底校准（0.15），是工程控制而非语义保证；
    # 最终「证据是否充分」仍由模型的 sufficient_evidence 判定。
    rag_min_vector_score: float = Field(default=0.15, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_chunk_overlap(self) -> "Settings":
        """重叠必须严格小于块大小，否则切块算法无法前进（死循环）。"""
        if self.document_chunk_overlap_chars >= self.document_chunk_size_chars:
            raise ValueError(
                "document_chunk_overlap_chars must be < document_chunk_size_chars "
                f"(got {self.document_chunk_overlap_chars} >= "
                f"{self.document_chunk_size_chars})"
            )
        return self

    @model_validator(mode="after")
    def _validate_embedding_provider(self) -> "Settings":
        """embedding provider 生产配置校验（fail closed，绝不静默降级）。

        - local-hash：默认零网络词汇向量（明确不是神经语义模型）；
        - fake：**生产配置显式选择即启动失败**（Phase 5C fail-closed）——
          测试仅通过直接构造 DeterministicFakeEmbeddingProvider 注入，
          不走生产配置；禁止任何环境通过配置启用 fake；
        - openai（= openai-compatible 语义路径）：真实语义 Embedding，
          必须显式配置 EMBEDDING_MODEL，否则启动失败（宁失败不静默降级）。
        """
        known = {"local-hash", "fake", "openai"}
        if self.embedding_provider not in known:
            raise ValueError(
                f"embedding_provider must be one of {sorted(known)} "
                f"(got {self.embedding_provider!r}); refusing to start"
            )
        if self.embedding_provider == "fake":
            raise ValueError(
                "embedding_provider=fake is test-only and refuses to start in "
                "production configuration; use local-hash (offline) or openai "
                "(explicitly configured semantic embedding)"
            )
        if self.embedding_provider == "openai" and not self.embedding_model.strip():
            raise ValueError(
                "embedding_provider=openai requires EMBEDDING_MODEL to be set "
                "(e.g. text-embedding-3-small); refusing to start without a model"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
