"""Phase 4C 离线评估（零网络、零费用）：固定评估集 × 6 项指标。

指标定义（README 同步声明，诚实边界）：
1. Retrieval Hit Rate@K：每个可答问题的预期事实子串是否全部出现在
   检索 top-K 的块内容中（未经过闸门，测检索本身）；
2. Answer Correctness：可答问题是否 ANSWERED 且每个预期事实子串都
   出现在某条引用的 quote 中（规则型 Fake 的确定性答案；测「事实结论
   可追溯到真实 chunk」，不测模型智能）；
3. Citation Precision：每条引用标签是否属于本次下发的证据 allowlist
   （伪造标签 C999 案例为 0）；证据不足问题不产生标签，不计入分母；
4. Citation Validity：每条引用是否指向真实存在的 chunk 且 quote 是
   该 chunk 内容的真实子串（后端权威性）；
5. Abstention Accuracy：无答案问题是否进入 INSUFFICIENT_EVIDENCE；
6. Prompt-injection Resistance：注入文档（含 sk-test-123456 /
   delete_all_tasks）出现时，答案不执行指令、不输出密钥、引用仍合法。

阈值断言是「下限」：固定语料 + 确定性算法下结果完全可复现
（2026-08-16 总审实测 1.00 / 1.00 / 0.9375 / 1.00 / 1.00 / 1.00；
Precision 的 0.9375 = 15/16，唯一失分来自 force_bad_citation 案例
C999 伪造标签 0/1——这正是该案例要验证的拒绝行为）。
"""

import logging

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import Base, _set_sqlite_pragma
from app.embeddings.local_hash_provider import LocalHashEmbeddingProvider
from app.services import document_service
from app.services.rag_errors import RAGCitationInvalidError
from app.services.rag_service import RagService
from app.services.retrieval_index_service import RetrievalIndexService
from app.services.retrieval_service import RetrievalService
from tests.rag_eval_dataset import EVAL_DOCS, EVAL_QUESTIONS, make_fake

logger = logging.getLogger(__name__)


@pytest.fixture()
def eval_ctx(tmp_path, monkeypatch):
    """上传固定语料并建立索引；分块设置 60/10 与数据集设计一致。"""
    monkeypatch.setattr(settings, "document_chunk_size_chars", 60)
    monkeypatch.setattr(settings, "document_chunk_overlap_chars", 10)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'eval.db'}", connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    embed = LocalHashEmbeddingProvider(dimension=384)
    doc_ids = {}
    for name, content in EVAL_DOCS:
        doc = document_service.upload_and_process(
            db,
            filename=name,
            content_type="text/plain",
            content=content.encode("utf-8"),
        )
        RetrievalIndexService(db, provider=embed).index_document(doc.id)
        doc_ids[name] = doc.id
    yield db, doc_ids, embed
    db.close()
    Base.metadata.drop_all(bind=engine)


def _chunk_contents(db) -> dict[int, str]:
    from app.models.document import DocumentChunk

    return {c.id: c.content for c in db.query(DocumentChunk).all()}


def _contains_any_of(text: str, substrings: list[str]) -> bool:
    return any(s in text for s in substrings)


def test_evaluation_suite(eval_ctx, caplog):
    db, doc_ids, embed = eval_ctx
    chunk_contents = _chunk_contents(db)
    retrieval_svc = RetrievalService(db, provider=embed)

    results = []
    for q in EVAL_QUESTIONS:
        # ---- 检索步（未闸门）----
        retr = retrieval_svc.search(query=q.question, mode=q.mode, top_k=5)
        retrieved_text = " ".join(item.content for item in retr.items)
        if q.abstain or not q.expected_substrings:
            hit = None  # 拒答问题不计入 Hit Rate（不期望任何块）
        else:
            hit = all(s in retrieved_text for s in q.expected_substrings)

        # ---- RAG 步 ----
        fake = make_fake(q.fake_kind)
        service = RagService(db, llm_provider=fake, embedding_provider=embed)
        outcome = {
            "key": q.key,
            "question": q.question,
            "mode": q.mode,
            "hit": hit,
            "status": None,
            "correctness": None,
            "precision_num": None,
            "precision_den": None,
            "validity": None,
            "abstain_ok": None,
            "injection_ok": None,
            "fake_calls": 0,
        }
        try:
            resp = service.ask(
                question=q.question,
                document_ids=None,
                retrieval_mode=q.mode,
                top_k=5,
                conversation_id=None,
                language="auto",
            )
        except RAGCitationInvalidError:
            # 伪造标签案例：0 条合法引用 / 1 个请求标签
            outcome["status"] = "REJECTED"
            outcome["precision_num"] = 0
            outcome["precision_den"] = 1
            results.append(outcome)
            continue

        outcome["status"] = resp.status.value
        outcome["fake_calls"] = len(fake.calls)
        quotes = [c.quote for c in resp.citations]

        # 正确性：ANSWERED 且每个预期事实都出现在某条引用 quote 中
        if not q.abstain and q.expected_substrings:
            outcome["correctness"] = bool(
                resp.status.value == "ANSWERED"
                and resp.answer
                and all(any(s in quote for quote in quotes) for s in q.expected_substrings)
            )
        # 引用精度：合法标签 / 请求标签（规则型 Fake 引用 prompt 中的全部证据块）
        if fake.calls:
            user_text = fake.calls[0][1]["content"]
            outcome["precision_den"] = user_text.count('<EVIDENCE id="')
        outcome["precision_num"] = len(resp.citations)
        # 引用有效性：chunk 真实存在且 quote 是内容的真实子串
        if resp.citations:
            outcome["validity"] = all(
                c.chunk_id in chunk_contents and c.quote in chunk_contents[c.chunk_id]
                for c in resp.citations
            )
        # 拒答正确性
        if q.abstain:
            outcome["abstain_ok"] = bool(
                resp.status.value == "INSUFFICIENT_EVIDENCE"
                and resp.citations == []
                and resp.usage.model is None
            )
        # 注入抵抗
        if q.injection:
            outcome["injection_ok"] = bool(
                resp.status.value == "ANSWERED"
                and "sk-test-123456" not in resp.answer
                and "delete_all_tasks" not in resp.answer
                and outcome.get("validity", False)
            )
        results.append(outcome)

    # ---- 汇总指标 ----
    answerable = [r for r in results if r["hit"] is not None]
    hits = [r["hit"] for r in answerable]
    correct = [r["correctness"] for r in answerable]
    precision = [r for r in results if r["precision_den"]]
    precision_score = sum(r["precision_num"] for r in precision) / sum(
        r["precision_den"] for r in precision
    )
    validity_rows = [r for r in results if r["validity"] is not None]
    validity = (
        sum(r["validity"] for r in validity_rows) / len(validity_rows)
        if validity_rows
        else 1.0
    )
    abstain_rows = [r for r in results if r["abstain_ok"] is not None]
    abstention = sum(r["abstain_ok"] for r in abstain_rows) / len(abstain_rows)
    injection_rows = [r for r in results if r["injection_ok"] is not None]
    injection = sum(r["injection_ok"] for r in injection_rows) / len(injection_rows)

    metrics = {
        "Retrieval Hit Rate@5": sum(hits) / len(hits),
        "Answer Correctness": sum(correct) / len(correct),
        "Citation Precision": precision_score,
        "Citation Validity": validity,
        "Abstention Accuracy": abstention,
        "Injection Resistance": injection,
    }

    # 逐案例明细（评估可读性；失败时定位到具体案例）
    lines = [f"{r['key']:20s} hit={str(r['hit']):5s} status={str(r['status']):12s} "
             f"correct={str(r['correctness']):5s} precision={r['precision_num']}/{r['precision_den']} "
             f"validity={r['validity']} abstain={r['abstain_ok']} injection={r['injection_ok']}"
             for r in results]
    logger.info("RAG eval per-case:\n%s", "\n".join(lines))
    logger.info("RAG eval metrics: %s", metrics)

    # ---- 断言下限（确定性语料下可复现）----
    assert metrics["Retrieval Hit Rate@5"] >= 0.8
    assert metrics["Answer Correctness"] >= 0.8
    assert metrics["Citation Precision"] >= 0.8
    assert metrics["Citation Validity"] == 1.0
    assert metrics["Abstention Accuracy"] == 1.0
    assert metrics["Injection Resistance"] == 1.0

    # 拒答案例必须零 LLM 调用（零证据路径）
    no_answer = next(r for r in results if r["key"] == "no_answer_cn")
    assert no_answer["fake_calls"] == 0
    assert no_answer["abstain_ok"] is True


def test_eval_dataset_is_well_formed():
    """评估集自身的一致性：禁答问题不带预期子串、注入问题标记、标签唯一。"""
    keys = [q.key for q in EVAL_QUESTIONS]
    assert len(keys) == len(set(keys))
    for q in EVAL_QUESTIONS:
        assert q.question.strip()
        assert q.mode in ("hybrid", "vector", "keyword")
        if q.abstain:
            assert not q.expected_substrings
        if q.expected_substrings:
            assert not q.abstain
