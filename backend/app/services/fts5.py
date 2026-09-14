"""SQLite FTS5 全文检索：建表、token 化、写入、查询（Phase 4B）。

设计决策：
- 普通虚拟表（非 external-content / contentless）：行数据自包含，
  rowid = chunk_id，支持 INSERT OR REPLACE 幂等重写；
- 无自动 trigger：FTS 由 retrieval_index_service 在明确事务边界内显式同步，
  逻辑清晰、可测试、可故障注入；
- 两列可搜索：content（原文，unicode61 tokenizer 处理英文/数字词）+
  cjk_grams（预生成的中文 n-gram(1-2) 序列——unicode61 会把连续中文段
  当作单个 token，「你好世界」与「你好，世界」互不命中；n-gram 列解决
  中文检索的粒度问题）；
- document_id 为 UNINDEXED 列：用于文档过滤（WHERE 参与，不进索引）；
- 安全：用户 query 绝不直接拼接进 SQL。MATCH 表达式只由安全正则产生的
  token 构成，每个 token 用双引号字面量包裹（FTS5 引号内无特殊字符）；
  所有 token 以 OR 连接（布尔候选生成，相关度由 bm25 排序保证）；
- 分数方向统一：-bm25()，越大越相关（final_score 单调一致）。

Phase 5A：token 化规则迁至 services/keyword_tokens.py（与 PG ILIKE 回退
共用）；本模块保持 FTS5 专属实现（建表/写入/查询）。
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.keyword_tokens import build_cjk_grams, scan_query_tokens
from app.services.retrieval_errors import RetrievalNotAvailableError

logger = logging.getLogger(__name__)

# FTS 表名（迁移与索引服务共用此单一来源）
FTS_TABLE = "document_chunks_fts"


def build_match_expression(query: str) -> str | None:
    """查询侧：安全 MATCH 表达式；无可搜索 token 返回 None。

    token 规则（共享 token 化，见 keyword_tokens.scan_query_tokens）：
    英文词 + CJK bigram（CJK 段只有单字时用 unigram）。bigram 比 unigram
    精确；OR 语义保证含任一 bigram 的 chunk 都能进入候选池，相关度交给
    bm25 排序。所有 token 由正则产生且双引号包裹，无法构造 FTS5 语法注入。
    """
    tokens = scan_query_tokens(query)
    if not tokens:
        return None
    return " OR ".join(f'"{t}"' for t in tokens)


def ensure_fts_table(db: Session) -> None:
    """确保 FTS 虚拟表存在（运行期幂等）；SQLite 不支持 FTS5 时给出稳定错误。"""
    try:
        db.execute(
            text(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5("
                "content, cjk_grams, document_id UNINDEXED)"
            )
        )
        db.commit()
    except Exception as exc:
        # 不含 SQL 细节；FTS5 缺失属于环境能力问题，给出稳定 503
        raise RetrievalNotAvailableError() from exc


def replace_document_rows(
    db: Session, document_id: int, rows: list[tuple[int, str, str]]
) -> None:
    """以 chunk 行整体重写某文档的 FTS 行（先删后插，事务内）。

    rows: [(chunk_id, content, cjk_grams), ...]。rowid = chunk_id，
    INSERT OR REPLACE 保证幂等；DELETE 先按文档清理，杜绝幽灵记录。
    """
    db.execute(text(f"DELETE FROM {FTS_TABLE} WHERE document_id = :doc_id"), {"doc_id": document_id})
    for chunk_id, content, cjk_grams in rows:
        db.execute(
            text(
                f"INSERT OR REPLACE INTO {FTS_TABLE}"
                "(rowid, content, cjk_grams, document_id) VALUES (:cid, :content, :grams, :doc_id)"
            ),
            {
                "cid": chunk_id,
                "content": content,
                "grams": cjk_grams,
                "doc_id": document_id,
            },
        )


def _fts_table_exists(db: Session) -> bool:
    """FTS 虚拟表是否存在（create_all 不建虚拟表，删除路径须容错）。"""
    return (
        db.execute(
            text(
                "SELECT 1 FROM sqlite_master "
                "WHERE type = 'table' AND name = :name"
            ),
            {"name": FTS_TABLE},
        ).scalar()
        is not None
    )


def delete_document_rows(db: Session, document_id: int) -> None:
    """删除某文档的全部 FTS 行（文档删除/重索引前调用，事务内）。

    幂等：FTS 表未创建（文档从未索引）时无操作。
    """
    if not _fts_table_exists(db):
        return
    db.execute(
        text(f"DELETE FROM {FTS_TABLE} WHERE document_id = :doc_id"),
        {"doc_id": document_id},
    )


def keyword_search(
    db: Session,
    *,
    match_expr: str,
    document_ids: list[int] | None,
    limit: int,
) -> list[dict]:
    """FTS5 关键词检索：按 -bm25 降序返回候选。

    返回 [{"chunk_id", "document_id", "score"}]，score = -bm25（越大越相关）。
    document_ids 过滤与 MATCH 同查询（参数绑定，无 SQL 拼接）。
    """
    params: dict = {"expr": match_expr, "limit": limit}
    where_docs = ""
    if document_ids:
        placeholders = ", ".join(f":doc_{i}" for i in range(len(document_ids)))
        where_docs = f" AND document_id IN ({placeholders})"
        params.update({f"doc_{i}": doc_id for i, doc_id in enumerate(document_ids)})

    sql = (
        f"SELECT rowid AS chunk_id, document_id, "
        f"-bm25({FTS_TABLE}) AS score "
        f"FROM {FTS_TABLE} "
        f"WHERE {FTS_TABLE} MATCH :expr{where_docs} "
        f"ORDER BY score DESC, document_id ASC, chunk_id ASC "
        f"LIMIT :limit"
    )
    rows = db.execute(text(sql), params).mappings().all()
    return [
        {"chunk_id": row["chunk_id"], "document_id": row["document_id"], "score": row["score"]}
        for row in rows
    ]
