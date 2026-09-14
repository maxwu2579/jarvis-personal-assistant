"""RAG 问答请求 / 响应 schema（Phase 4C）。

安全与权威性设计：
- 请求严格模式（extra="forbid"）：空白 question、重复 document_ids、非法
  mode/top_k/language 直接 422 VALIDATION_ERROR；
- 模型输出（RAGModelOutput）只含 answer / cited_labels / sufficient_evidence，
  **不含任何权威字段**（document_id、chunk_id、title、quote、offset 一律由
  后端根据引用标签从数据库生成）；模型伪造标题/偏移/引用文本不生效；
- RAGModelOutput 解析采用 extra="ignore"：真实模型可能多带无关字段，宽松
  解析（多出的字段没有权威性，被忽略），缺字段/类型错仍是硬校验失败；
- 证据不足不是错误：HTTP 200 + status=INSUFFICIENT_EVIDENCE + 固定拒答文案。
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.retrieval import SearchMode

QUESTION_MAX = 2000
ANSWER_MAX = 4000
CITATION_LABEL_RE = r"^C\d+$"


class RAGLanguage(str, Enum):
    """回答语言：auto 跟随问题语言（由 system prompt 指示模型）。"""

    auto = "auto"
    zh = "zh"
    en = "en"


class RAGAskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(..., max_length=QUESTION_MAX)
    # 限定检索范围：None = 全部已索引文档；重复 id 拒绝；上限 50（防超大 IN 子句）
    document_ids: list[int] | None = Field(default=None, max_length=50)
    retrieval_mode: SearchMode = SearchMode.hybrid
    top_k: int = Field(default=5, ge=1, le=10)
    # 可选对话集成：提供时按既有聊天事务规则保存 USER/ASSISTANT 消息
    conversation_id: int | None = Field(default=None)
    language: RAGLanguage = RAGLanguage.auto

    @field_validator("question")
    @classmethod
    def _question_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value

    @field_validator("document_ids")
    @classmethod
    def _no_duplicates(cls, value: list[int] | None) -> list[int] | None:
        if value is None:
            return value
        seen: set[int] = set()
        for doc_id in value:
            if doc_id in seen:
                raise ValueError("document_ids must not contain duplicates")
            seen.add(doc_id)
        return value


class RAGModelOutput(BaseModel):
    """模型输出的最小契约：答案 + 引用标签 + 是否证据充分。

    引用标签只引用后端下发的证据块标签（C1、C2……）；allowlist 之外的标签
    一律拒绝（RAG_CITATION_INVALID）。权威引用信息全部由后端生成。
    """

    # extra="ignore"：真实模型可能输出多余字段，忽略（无权威性）；缺字段仍失败
    model_config = ConfigDict(extra="ignore")

    answer: str = Field(min_length=1, max_length=ANSWER_MAX)
    cited_labels: list[str] = Field(default_factory=list)
    sufficient_evidence: bool = False

    @field_validator("answer")
    @classmethod
    def _answer_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("answer must not be blank")
        return stripped

    @field_validator("cited_labels")
    @classmethod
    def _strip_labels(cls, value: list[str]) -> list[str]:
        cleaned = []
        for label in value:
            if not isinstance(label, str):
                raise ValueError("cited_labels must be strings")
            stripped = label.strip()
            if stripped:
                cleaned.append(stripped)
        return cleaned


class CitationOut(BaseModel):
    """一条引用：全部字段由后端基于数据库生成，不信任模型输出。"""

    citation_id: str  # C1、C2……
    document_id: int
    document_title: str
    chunk_id: int
    chunk_index: int
    # chunk 在原始文档中的字符偏移区间（来自 DocumentChunk，权威值）
    char_start: int
    char_end: int
    # 数据库中该 chunk 的真实连续子串（确定性截取，绝不来自模型）
    quote: str
    retrieval_score: float
    retrieval_mode: str


class RAGAnswerStatus(str, Enum):
    ANSWERED = "ANSWERED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class RAGRetrievalMeta(BaseModel):
    """检索元数据：candidate_count = 预排候选数，returned_count = 进入上下文的块数。"""

    mode: str
    candidate_count: int
    returned_count: int


class RAGUsageOut(BaseModel):
    """调用使用量：未调用模型（零证据拒答）时 model/tokens 为 None。"""

    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int | None


class RAGAnswerResponse(BaseModel):
    answer: str
    status: RAGAnswerStatus
    citations: list[CitationOut]
    retrieval: RAGRetrievalMeta
    usage: RAGUsageOut
    conversation_id: int | None
    user_message_id: int | None
    assistant_message_id: int | None
