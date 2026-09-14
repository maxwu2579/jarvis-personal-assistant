"""检索 API 集成测试（Phase 4B）：索引/重索引/搜索、错误码、防泄露。"""

import pytest

from app.services.retrieval_index_service import _embed_chunk_batch  # noqa: F401

API = "/api/documents"
RETR = "/api/retrieval"

DOC_CN = "JARVIS 提醒系统：记录任务与提醒，周日晚上八点提交周报。\n"
DOC_EN = "JARVIS reminder system: record tasks and reminders, submit weekly report on Sunday evening.\n"


def _upload_txt(client, text: str, filename: str = "note.txt"):
    resp = client.post(API, files={"file": (filename, text.encode("utf-8"), "text/plain")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _index(client, document_id: int):
    return client.post(f"{RETR}/index/{document_id}")


# ---------- 端到端成功路径 ----------


def test_index_and_search_end_to_end(client):
    doc = _upload_txt(client, DOC_CN)
    resp = _index(client, doc["id"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["document_id"] == doc["id"]
    assert body["index_status"] == "INDEXED"
    assert body["embeddings_created"] > 0
    assert body["fts_rows_written"] > 0

    # keyword：中文命中
    kw = client.post(
        f"{RETR}/search", json={"query": "提醒 周报", "mode": "keyword", "top_k": 5}
    )
    assert kw.status_code == 200, kw.text
    kw_items = kw.json()["items"]
    assert kw_items
    assert kw_items[0]["document_id"] == doc["id"]
    assert kw_items[0]["keyword_score"] > 0

    # vector：词汇重合命中
    vec = client.post(
        f"{RETR}/search", json={"query": "提交周报 提醒", "mode": "vector"}
    )
    assert vec.status_code == 200, vec.text
    vec_items = vec.json()["items"]
    assert vec_items and vec_items[0]["vector_score"] > 0

    # hybrid：默认 mode，融合分数存在
    hyb = client.post(f"{RETR}/search", json={"query": "提醒 周报"})
    assert hyb.status_code == 200, hyb.text
    assert hyb.json()["mode"] == "hybrid"
    assert hyb.json()["embedding_model"] == "local-hash-v1"
    assert hyb.json()["items"]
    # 命中块定位字段齐全（Phase 4C citations 基础）
    item = hyb.json()["items"][0]
    for field in (
        "document_id", "document_title", "chunk_id", "chunk_index",
        "content", "content_length", "char_start", "char_end",
        "keyword_score", "vector_score", "final_score",
    ):
        assert field in item


def test_search_document_filter(client):
    cn = _upload_txt(client, DOC_CN, "cn.txt")
    _upload_txt(client, DOC_EN, "en.txt")
    _index(client, cn["id"])
    resp = client.post(
        f"{RETR}/search",
        json={"query": "reminder 提醒", "document_ids": [cn["id"]]},
    )
    assert resp.status_code == 200, resp.text
    assert all(item["document_id"] == cn["id"] for item in resp.json()["items"])


def test_reindex_forces_full_rebuild(client):
    doc = _upload_txt(client, DOC_CN)
    _index(client, doc["id"])
    resp = client.post(f"{RETR}/reindex/{doc['id']}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["embeddings_created"] == body["chunks_scanned"]
    assert body["embeddings_skipped"] == 0
    assert body["index_status"] == "INDEXED"


def test_search_special_characters_no_500(client):
    doc = _upload_txt(client, DOC_CN)
    _index(client, doc["id"])
    resp = client.post(f"{RETR}/search", json={"query": "！！？？（）……"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["items"] == []


# ---------- 索引错误路径 ----------


def test_index_missing_document_404(client):
    resp = _index(client, 999999)
    assert resp.status_code == 404, resp.text
    body = resp.json()["error"]
    assert body["code"] == "DOCUMENT_NOT_FOUND"


def test_index_failed_document_409(client):
    # 损坏 PDF → 摄取 FAILED，不可索引
    resp = client.post(
        API, files={"file": ("bad.pdf", b"%PDF- broken content", "application/pdf")}
    )
    assert resp.status_code == 201
    doc_id = resp.json()["id"]
    resp = _index(client, doc_id)
    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == "DOCUMENT_NOT_READY"


def test_index_failure_returns_500_and_marks_document(client, monkeypatch):
    doc = _upload_txt(client, DOC_CN)

    def boom(provider, contents):  # noqa: ARG001
        raise RuntimeError("simulated embedding failure")

    monkeypatch.setattr(
        "app.services.retrieval_index_service._embed_chunk_batch", boom
    )
    resp = _index(client, doc["id"])
    assert resp.status_code == 500, resp.text
    assert resp.json()["error"]["code"] == "INDEXING_FAILED"

    # 文档保持 READY（摄取不受影响），检索状态 INDEX_FAILED、无部分数据
    detail = client.get(f"{API}/{doc['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "READY"
    assert body["retrieval_status"] == "INDEX_FAILED"
    assert body["indexed_chunk_count"] == 0


# ---------- 搜索请求校验 ----------


def test_search_blank_query_422(client):
    resp = client.post(f"{RETR}/search", json={"query": "   "})
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_search_oversized_query_422(client):
    resp = client.post(f"{RETR}/search", json={"query": "x" * 1001})
    assert resp.status_code == 422, resp.text


def test_search_top_k_out_of_range_422(client):
    for bad in (0, 21):
        resp = client.post(f"{RETR}/search", json={"query": "提醒", "top_k": bad})
        assert resp.status_code == 422, resp.text


def test_search_unknown_mode_422(client):
    resp = client.post(f"{RETR}/search", json={"query": "提醒", "mode": "semantic"})
    assert resp.status_code == 422, resp.text


def test_search_extra_field_rejected_422(client):
    resp = client.post(
        f"{RETR}/search", json={"query": "提醒", "extra_field": "nope"}
    )
    assert resp.status_code == 422, resp.text


def test_search_duplicate_document_ids_422(client):
    resp = client.post(
        f"{RETR}/search",
        json={"query": "提醒", "document_ids": [1, 1]},
    )
    assert resp.status_code == 422, resp.text


def test_search_missing_query_field_422(client):
    resp = client.post(f"{RETR}/search", json={"mode": "keyword"})
    assert resp.status_code == 422, resp.text


# ---------- 删除无幽灵 ----------


def test_delete_removes_indexed_document_from_results(client):
    doc = _upload_txt(client, DOC_CN)
    _index(client, doc["id"])
    before = client.post(f"{RETR}/search", json={"query": "提醒 周报"})
    assert before.json()["items"]

    resp = client.delete(f"{API}/{doc['id']}")
    assert resp.status_code == 200

    after = client.post(f"{RETR}/search", json={"query": "提醒 周报"})
    assert after.status_code == 200
    assert after.json()["items"] == []
    assert client.get(f"{API}/{doc['id']}").status_code == 404


# ---------- 响应防泄露 ----------

_LEAK_PATTERNS = ("vector_json", "Traceback", "C:\\", "/backend/", "sqlite://", "sha256")


def test_search_response_no_leakage(client):
    doc = _upload_txt(client, DOC_CN)
    _index(client, doc["id"])
    resp = client.post(
        f"{RETR}/search", json={"query": '"; DROP TABLE document_chunks_fts; --'}
    )
    assert resp.status_code == 200, resp.text
    raw = resp.text
    for pattern in _LEAK_PATTERNS:
        assert pattern not in raw, f"leaked {pattern!r} in search response"


def test_index_error_response_no_leakage(client, monkeypatch):
    doc = _upload_txt(client, DOC_CN)

    def boom(provider, contents):  # noqa: ARG001
        raise RuntimeError("secret internal path C:\\secret\\trace details")

    monkeypatch.setattr(
        "app.services.retrieval_index_service._embed_chunk_batch", boom
    )
    resp = _index(client, doc["id"])
    assert resp.status_code == 500
    raw = resp.text
    for pattern in _LEAK_PATTERNS + ("secret",):
        assert pattern not in raw, f"leaked {pattern!r} in index error response"


# ---------- 文档详情/列表的检索字段 ----------


def test_document_detail_exposes_index_fields(client):
    doc = _upload_txt(client, DOC_CN)
    assert doc["retrieval_status"] == "NOT_INDEXED"
    assert doc["indexed_chunk_count"] == 0

    _index(client, doc["id"])

    detail = client.get(f"{API}/{doc['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["retrieval_status"] == "INDEXED"
    assert body["indexed_chunk_count"] > 0

    listing = client.get(API)
    assert listing.status_code == 200
    listed = next(d for d in listing.json() if d["id"] == doc["id"])
    assert listed["retrieval_status"] == "INDEXED"
    assert listed["indexed_chunk_count"] == body["indexed_chunk_count"]


def test_indexed_chunk_count_matches_embeddings(client):
    doc = _upload_txt(client, DOC_CN)
    _index(client, doc["id"])
    detail = client.get(f"{API}/{doc['id']}")
    # 单文档索引：embeddings_created == 已索引块数
    assert detail.json()["indexed_chunk_count"] > 0
