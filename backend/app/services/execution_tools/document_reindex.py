"""documents.reindex 执行工具 handler（Phase 7.1）。

在 Execution 执行域内复用既有检索索引服务（RetrievalIndexService）：
- 复用语义：强制重建（先清空旧向量行再全部重建，事务原子，
  NOT_INDEXED/INDEXING/INDEXED|INDEX_FAILED 状态机、stale 替换、
  孤儿清理、关键词行重写）——全部沿用既有服务实现，不复制、不新起
  索引框架 / 向量数据库 / 后台进程；
- destructive + requires_confirmation + replay_safe=False：确认门
  （GRANTED / REJECTED / EXPIRED 裁决、输入 hash 绑定）、崩溃恢复拒绝
  重放——全部由执行域编排器（execution_steps 的 confirmation gate /
  恢复规则）负责，本 handler 不绕过、不决策；
- provider：走服务默认（settings.embedding_provider，默认 local-hash
  零网络；生产配置语义 provider）——handler 不选择 embedding 后端，
  语义与检索服务一致；测试环境默认配置即真实离线路径；
- 错误语义：RetrievalError（DOCUMENT_NOT_FOUND / DOCUMENT_NOT_READY /
  INDEXING_FAILED…）带稳定 code 直接传播，编排器映射为 step 失败 code
  （无 code 的异常 → EXEC_HANDLER_ERROR 兜底）；
- session 注入同 reminder_send：模块级 _session_factory（默认
  app.core.database.SessionLocal），测试 monkeypatch 替换注入测试库。
  服务内部自行管理 commit / rollback，handler 只负责关闭 session。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.database import SessionLocal
from app.services.retrieval_index_service import (
    IndexStats,
    RetrievalIndexService,
)

# 可注入边界（测试 monkeypatch 替换；生产为零配置）。
_session_factory = SessionLocal


class DocumentReindexInput(BaseModel):
    """documents.reindex 输入（extra="forbid"，白名单字段）。"""

    model_config = ConfigDict(extra="forbid")

    document_id: int


class DocumentReindexOutput(BaseModel):
    """documents.reindex 输出：与 IndexStats.to_dict() 字段一致
    （extra="forbid"；落盘前再经 sanitize_result_value）。"""

    model_config = ConfigDict(extra="forbid")

    documents_scanned: int
    chunks_scanned: int
    embeddings_created: int
    embeddings_updated: int
    embeddings_skipped: int
    embeddings_stale: int
    deleted_orphans: int
    fts_rows_written: int
    failures: int


def document_reindex_handler(input_value: dict[str, Any]) -> dict[str, Any]:
    document_id = int(input_value["document_id"])
    db = _session_factory()
    try:
        stats: IndexStats = RetrievalIndexService(db).reindex_document(document_id)
        return stats.to_dict()
    finally:
        db.close()
