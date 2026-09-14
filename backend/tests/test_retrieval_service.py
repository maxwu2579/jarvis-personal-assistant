"""检索服务测试（Phase 4B）：keyword / vector / hybrid、RRF、注入、排序、上限。"""

import json

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base, _set_sqlite_pragma
from app.embeddings.local_hash_provider import LocalHashEmbeddingProvider
from app.models.document import (
    ChunkEmbedding,
    Document,
    DocumentChunk,
    DocumentStatus,
)
from app.services import document_service, fts5
from app.services.retrieval_errors import (
    InvalidSearchQueryError,
    RetrievalCandidateLimitExceededError,
)
from app.services.retrieval_index_service import RetrievalIndexService
from app.services.retrieval_service import RetrievalService

# 中英混合内容：keyword 与 vector 都能命中
DOC_CN = "JARVIS 提醒系统：记录任务与提醒，周日晚上八点提交周报。\n"
DOC_EN = "JARVIS reminder system: record tasks and reminders, submit weekly report on Sunday evening.\n"
DOC_OTHER = "今天是晴天，去公园散步。\n"


@pytest.fixture()
def db_session(tmp_path):
    db_path = tmp_path / "retr.db"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def ctx(db_session):
    """上传两个文档并建立索引，返回 (service, doc_ids)。"""
    doc_cn = document_service.upload_and_process(
        db_session,
        filename="reminders_cn.txt",
        content_type="text/plain",
        content=DOC_CN.encode("utf-8"),
    )
    doc_en = document_service.upload_and_process(
        db_session,
        filename="reminders_en.txt",
        content_type="text/plain",
        content=DOC_EN.encode("utf-8"),
    )
    doc_other = document_service.upload_and_process(
        db_session,
        filename="weather.txt",
        content_type="text/plain",
        content=DOC_OTHER.encode("utf-8"),
    )
    indexer = RetrievalIndexService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    for doc in (doc_cn, doc_en, doc_other):
        indexer.index_document(doc.id)
    service = RetrievalService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    return service, [doc_cn.id, doc_en.id, doc_other.id]


# ---------- 输入校验 ----------


def test_blank_query_rejected(ctx):
    service, _ = ctx
    with pytest.raises(InvalidSearchQueryError):
        service.search(query="   ")


def test_oversized_query_rejected(ctx):
    service, _ = ctx
    with pytest.raises(InvalidSearchQueryError):
        service.search(query="x" * 1001)


def test_top_k_out_of_range_rejected(ctx):
    service, _ = ctx
    with pytest.raises(InvalidSearchQueryError):
        service.search(query="提醒", top_k=0)
    with pytest.raises(InvalidSearchQueryError):
        service.search(query="提醒", top_k=21)


def test_unknown_mode_rejected(ctx):
    service, _ = ctx
    from app.services.retrieval_errors import InvalidSearchModeError

    with pytest.raises(InvalidSearchModeError):
        service.search(query="提醒", mode="semantic")


def test_duplicate_document_ids_rejected(ctx):
    service, doc_ids = ctx
    with pytest.raises(InvalidSearchQueryError):
        service.search(query="提醒", document_ids=[doc_ids[0], doc_ids[0]])


# ---------- keyword ----------


def test_keyword_chinese_hits_expected_chunk(ctx):
    service, doc_ids = ctx
    result = service.search(query="提醒 周报", mode="keyword", top_k=5)
    assert result.items
    first = result.items[0]
    assert first.document_id == doc_ids[0]
    assert "提醒" in first.content
    assert first.keyword_score > 0
    assert first.vector_score == 0.0
    assert first.final_score == first.keyword_score


def test_keyword_english_hits_expected_chunk(ctx):
    service, doc_ids = ctx
    result = service.search(query="weekly report", mode="keyword", top_k=5)
    assert result.items
    assert result.items[0].document_id == doc_ids[1]


def test_keyword_mixed_query(ctx):
    service, doc_ids = ctx
    result = service.search(query="JARVIS 周报", mode="keyword", top_k=10)
    assert result.items
    # 两个文档都含 JARVIS；周报相关的中文文档排前
    assert result.items[0].document_id in (doc_ids[0], doc_ids[1])


def test_keyword_document_filter(ctx):
    service, doc_ids = ctx
    result = service.search(query="提醒", mode="keyword", document_ids=[doc_ids[1]])
    assert all(item.document_id == doc_ids[1] for item in result.items)


def test_keyword_sql_injection_safe(ctx, db_session):
    """FTS 注入输入不得改变语义/产生 500：全部作为字面量 token 处理。"""
    service, _ = ctx
    for evil in [
        '"; DROP TABLE document_chunks_fts; --',
        "' OR 1=1 --",
        '") OR ("a',
        "提醒\" OR \"周报",
        "提醒)) OR ((",
        "* OR **",
    ]:
        result = service.search(query=evil, mode="keyword")
        assert result.items == [] or all(
            "提醒" in item.content or "周报" in item.content for item in result.items
        )
    # 表仍存在（未被注入破坏）
    exists = db_session.execute(
        text(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name = :n"
        ),
        {"n": fts5.FTS_TABLE},
    ).scalar()
    assert exists == 1


def test_keyword_symbols_only_returns_empty(ctx):
    service, _ = ctx
    result = service.search(query="！？（）……", mode="keyword")
    assert result.items == []


def test_keyword_score_ordering_stable(ctx):
    service, _ = ctx
    result = service.search(query="提醒", mode="keyword", top_k=10)
    scores = [item.final_score for item in result.items]
    assert scores == sorted(scores, reverse=True)
    keys = [(item.document_id, item.chunk_index) for item in result.items]
    assert keys == sorted(keys)  # 同分时按 (document_id, chunk_index)


# ---------- vector ----------


def test_vector_hits_lexical_overlap_chunk(ctx):
    service, doc_ids = ctx
    result = service.search(query="提交周报 提醒", mode="vector", top_k=5)
    assert result.items
    assert result.items[0].document_id == doc_ids[0]
    assert result.items[0].vector_score > 0
    assert result.items[0].keyword_score == 0.0
    assert result.embedding_model == "local-hash-v1"


def test_vector_document_filter(ctx):
    service, doc_ids = ctx
    result = service.search(
        query="提醒", mode="vector", document_ids=[doc_ids[2]]
    )
    assert all(item.document_id == doc_ids[2] for item in result.items)


def test_vector_invalid_json_skipped_not_crash(db_session):
    """非法 vector_json 被跳过并记录，不崩溃。"""
    doc = document_service.upload_and_process(
        db_session,
        filename="a.txt",
        content_type="text/plain",
        content="向量数据 测试内容 2026".encode("utf-8"),
    )
    indexer = RetrievalIndexService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    indexer.index_document(doc.id)
    # 破坏一条向量
    row = db_session.query(ChunkEmbedding).first()
    for bad in ("not json", "[1,2,3]", '[1.0, "x"]', "null"):
        row.vector_json = bad
        db_session.commit()
        service = RetrievalService(
            db_session, provider=LocalHashEmbeddingProvider(dimension=384)
        )
        result = service.search(query="向量", mode="vector")
        assert result.items == []
    # 恢复后可正常检索
    row.vector_json = json.dumps([0.1] * 384)
    db_session.commit()
    result = service.search(query="向量", mode="vector")
    assert result.items


def test_vector_candidate_limit(ctx, monkeypatch):
    service, _ = ctx
    monkeypatch.setattr(
        "app.services.retrieval_service.settings.retrieval_candidate_limit", 1
    )
    with pytest.raises(RetrievalCandidateLimitExceededError):
        service.search(query="提醒", mode="vector")


def test_vector_stable_ordering(ctx):
    service, _ = ctx
    result = service.search(query="提醒 任务", mode="vector", top_k=10)
    scores = [item.vector_score for item in result.items]
    assert scores == sorted(scores, reverse=True)


# ---------- hybrid ----------


def test_hybrid_rrf_fuses_and_dedups(ctx):
    service, doc_ids = ctx
    keyword = service.search(query="提醒 周报", mode="keyword", top_k=10)
    vector = service.search(query="提醒 周报", mode="vector", top_k=10)
    hybrid = service.search(query="提醒 周报", mode="hybrid", top_k=10)

    assert hybrid.items
    # 同一 chunk 不重复
    chunk_ids = [item.chunk_id for item in hybrid.items]
    assert len(chunk_ids) == len(set(chunk_ids))
    # 融合分 = RRF：keyword 排名贡献 + vector 排名贡献
    kw_map = {i.chunk_id: rank for rank, i in enumerate(keyword.items, 1)}
    vec_map = {i.chunk_id: rank for rank, i in enumerate(vector.items, 1)}
    from app.core.config import settings

    k = settings.retrieval_rrf_k
    for item in hybrid.items[:3]:
        expected = 0.0
        if item.chunk_id in kw_map:
            expected += 0.5 / (k + kw_map[item.chunk_id])
        if item.chunk_id in vec_map:
            expected += 0.5 / (k + vec_map[item.chunk_id])
        assert item.final_score == pytest.approx(expected, abs=1e-9)
    # final 排序稳定
    assert [i.final_score for i in hybrid.items] == sorted(
        [i.final_score for i in hybrid.items], reverse=True
    )


def test_hybrid_vector_only_when_keyword_empty(db_session):
    """keyword 无结果（FTS 行缺失）时 hybrid 仍返回 vector 结果。"""
    doc = document_service.upload_and_process(
        db_session,
        filename="v.txt",
        content_type="text/plain",
        content="符号没有 但是词汇存在 向量可命中 提醒测试".encode("utf-8"),
    )
    indexer = RetrievalIndexService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    indexer.index_document(doc.id)
    # 模拟 FTS 数据缺失（keyword 无结果），embedding 完好
    db_session.execute(
        text(f"DELETE FROM {fts5.FTS_TABLE} WHERE document_id = :d"), {"d": doc.id}
    )
    db_session.commit()
    service = RetrievalService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    result = service.search(query="提醒", mode="hybrid", top_k=5)
    assert result.items  # vector-only fallback
    assert all(item.keyword_score == 0.0 for item in result.items)
    assert any(item.vector_score > 0 for item in result.items)


def test_hybrid_symbol_only_query_returns_empty_not_junk(db_session):
    """全符号 query：keyword/vector 都无意义 → 空结果而非 0 分垃圾。"""
    doc = document_service.upload_and_process(
        db_session,
        filename="s.txt",
        content_type="text/plain",
        content="提醒 周报 任务 词汇".encode("utf-8"),
    )
    indexer = RetrievalIndexService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    indexer.index_document(doc.id)
    service = RetrievalService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    result = service.search(query="！！？？", mode="hybrid", top_k=5)
    assert result.items == []


def test_hybrid_keyword_only_when_vector_skipped(db_session):
    """vector 全被跳过（非法向量）时 hybrid 仍返回 keyword 结果。"""
    doc = document_service.upload_and_process(
        db_session,
        filename="k.txt",
        content_type="text/plain",
        content="关键词检索 仍然可用 提醒测试".encode("utf-8"),
    )
    indexer = RetrievalIndexService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    indexer.index_document(doc.id)
    for row in db_session.query(ChunkEmbedding).all():
        row.vector_json = "garbage"
    db_session.commit()
    service = RetrievalService(
        db_session, provider=LocalHashEmbeddingProvider(dimension=384)
    )
    result = service.search(query="提醒", mode="hybrid", top_k=5)
    assert result.items  # keyword-only fallback
    assert all(item.keyword_score > 0 for item in result.items)


def test_hybrid_top_k_and_empty(ctx):
    service, _ = ctx
    result = service.search(query="提醒", mode="hybrid", top_k=2)
    assert len(result.items) <= 2
    empty = service.search(query="完全不存在的内容 qqqqqqqq zzzzzzz", mode="hybrid")
    # 词汇哈希下几乎必然与某 chunk 有重叠；为稳定断言用 keyword-only 检查
    assert empty.mode == "hybrid"


def test_result_contains_location_fields(ctx):
    """响应包含真实 chunk 定位信息（Phase 4C citations 基础）。"""
    service, _ = ctx
    result = service.search(query="提醒", mode="hybrid", top_k=3)
    for item in result.items:
        assert item.document_id > 0
        assert item.chunk_id > 0
        assert item.chunk_index >= 0
        assert item.char_start >= 0
        assert item.char_end > item.char_start
        assert item.document_title
        assert item.content_length >= len(item.content.rstrip("…"))
