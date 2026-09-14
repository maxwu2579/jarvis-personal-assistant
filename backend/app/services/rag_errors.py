"""RAG 领域错误（Phase 4C）。

稳定错误码 → HTTP 状态码映射（main.py 全局 handler 使用）：
- RAG_RESPONSE_INVALID  502  模型输出无法构成合法 RAG 输出（非 JSON、缺字段、
  空白 answer 等）；绝不泄露模型原始输出
- RAG_CITATION_INVALID  502  引用标签不在本次下发的证据 allowlist 中
  （含 C999 之类伪造标签），或声称证据充分却零引用

证据不足不是错误：HTTP 200 + status=INSUFFICIENT_EVIDENCE（见 rag_service）。
文案全部 sanitized：不含路径、Traceback、API Key、完整 system prompt、
模型原始输出。
"""

RAG_ERROR_STATUS: dict[str, int] = {
    "RAG_RESPONSE_INVALID": 502,
    "RAG_CITATION_INVALID": 502,
}


class RAGError(Exception):
    """RAG 领域错误基类。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class RAGResponseInvalidError(RAGError):
    """模型输出二次验证失败（非 JSON / 结构不符 / 空白 answer）。

    error_detail 只含字段定位与规则文本（可审计、脱敏），不含原始输出内容。
    """

    def __init__(self, error_detail: str):
        self.error_detail = error_detail
        super().__init__(
            "RAG_RESPONSE_INVALID",
            "LLM returned an invalid RAG answer",
        )


class RAGCitationInvalidError(RAGError):
    """引用标签校验失败：引用本次未提供的标签，或声称充分却无引用。

    invalid_labels 只含标签字符串（如 "C999"），不包含模型原始输出全文。
    """

    def __init__(self, *, invalid_labels: list[str] | None = None, no_labels: bool = False):
        self.invalid_labels = invalid_labels or []
        self.no_labels = no_labels
        super().__init__(
            "RAG_CITATION_INVALID",
            "LLM cited evidence that was not provided to it",
        )
