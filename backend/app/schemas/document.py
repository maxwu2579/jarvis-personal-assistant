"""Document / DocumentChunk 的响应 schema（Phase 4A）。

安全约定：chunks 接口只返回内容预览（截断约 300 字符），完整块内容
不经 HTTP 暴露；完整内容在本阶段仅存于数据库，供未来 4B 检索使用。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_serializer

from app.models.document import DocumentIndexStatus, DocumentStatus
from app.schemas.common import serialize_utc

# 块内容预览长度上限（字符）
CHUNK_PREVIEW_LIMIT = 300


class DocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    original_filename: str
    content_type: str
    size_bytes: int
    sha256: str
    status: DocumentStatus
    error_code: str | None
    error_message: str | None
    # Phase 4B：检索索引状态（来自 Document.index_status，独立于摄取状态）
    retrieval_status: DocumentIndexStatus = Field(validation_alias="index_status")
    # 已索引块数（路由填充，单聚合查询避免 N+1）
    indexed_chunk_count: int = 0
    created_at: datetime
    updated_at: datetime

    @field_serializer("created_at", "updated_at")
    def _ser_utc(self, value: datetime) -> str:
        return serialize_utc(value)


class DocumentChunkOut(BaseModel):
    """块响应：content 为预览（超长截断并标注长度），绝不返回完整原文。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    document_id: int
    chunk_index: int
    content: str
    char_start: int
    char_end: int
    token_estimate: int

    @computed_field
    @property
    def content_length(self) -> int:
        """完整块长度（实例化时基于原始 content 计算；序列化时 content 已截断）。"""
        return len(self.content)

    @field_serializer("content")
    def _preview(self, value: str) -> str:
        if len(value) <= CHUNK_PREVIEW_LIMIT:
            return value
        return value[:CHUNK_PREVIEW_LIMIT] + "…"
