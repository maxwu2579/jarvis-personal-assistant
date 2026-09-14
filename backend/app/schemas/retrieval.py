"""检索 API 请求/响应 schema（Phase 4B）。

- SearchRequest 严格模式（extra="forbid"）：空白 query、重复 document_ids
  直接 422 VALIDATION_ERROR（归一错误结构，与全应用一致）；
- SearchResponse 只含命中的块定位信息与分数，不暴露任何索引内部结构
  （char_start/char_end/chunk_index 是 Phase 4C citations 的基础）。
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SearchMode(str, Enum):
    keyword = "keyword"
    vector = "vector"
    hybrid = "hybrid"


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., max_length=1000)
    mode: SearchMode = SearchMode.hybrid
    top_k: int = Field(default=5, ge=1, le=20)
    # 上限 50：防止超大列表构造巨型 IN 子句（SQLite 变量数限制 → 500）
    document_ids: list[int] | None = Field(default=None, max_length=50)

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("query must not be blank")
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


class SearchItemOut(BaseModel):
    """单个命中块：定位信息 + 三组分数（keyword / vector / final=RRF 融合）。"""

    document_id: int
    document_title: str
    chunk_id: int
    chunk_index: int
    content: str
    content_length: int
    char_start: int
    char_end: int
    keyword_score: float
    vector_score: float
    final_score: float


class SearchResponse(BaseModel):
    query: str
    mode: str
    items: list[SearchItemOut]
    total_candidates: int
    embedding_model: str
