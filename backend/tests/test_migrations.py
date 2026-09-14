"""Alembic 迁移测试：初始迁移必须能从空数据库执行并生成完整 schema。"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

BACKEND_DIR = Path(__file__).resolve().parents[1]

EXPECTED_COLUMNS = {
    "id",
    "title",
    "description",
    "status",
    "due_at",
    "created_at",
    "updated_at",
}

# head（4C）的完整表集合：Phase 0 tasks + Phase 1 对话/提案 + Phase 3 提醒/通知
# + Phase 4A 文档与块 + Phase 4B 检索（chunk_embeddings；FTS 为虚拟表，另查）
# + Phase 4C RAG 审计（rag_answers）
EXPECTED_TABLES = {
    "tasks",
    "conversations",
    "messages",
    "task_proposals",
    "reminders",
    "notifications",
    "documents",
    "document_chunks",
    "chunk_embeddings",
    "rag_answers",
}


def _make_config(db_file: Path) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def test_initial_migration_upgrades_empty_database(tmp_path):
    db_file = tmp_path / "migrated.db"
    command.upgrade(_make_config(db_file), "head")

    engine = create_engine(f"sqlite:///{db_file}")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert EXPECTED_TABLES <= tables
    assert "alembic_version" in tables

    columns = {col["name"] for col in inspector.get_columns("tasks")}
    assert columns == EXPECTED_COLUMNS


def test_migrated_database_accepts_task_rows(tmp_path):
    """迁移后的库可以直接承载应用数据（证明迁移路径完整可用）。"""
    db_file = tmp_path / "migrated.db"
    command.upgrade(_make_config(db_file), "head")

    engine = create_engine(f"sqlite:///{db_file}")
    from sqlalchemy.orm import sessionmaker

    from app.models.task import Task, TaskStatus

    session = sessionmaker(bind=engine)()
    task = Task(title="via migration", status=TaskStatus.DRAFT.value)
    session.add(task)
    session.commit()
    session.refresh(task)
    assert task.id is not None
    assert task.status == TaskStatus.DRAFT.value
    session.close()


def test_upgrade_from_previous_revision_preserves_existing_data(tmp_path):
    """Phase 1 升级路径：已有 001 数据的数据库升级到 head 后数据不丢、新表可用。"""
    db_file = tmp_path / "legacy.db"
    cfg = _make_config(db_file)

    # 1) 先停在旧 revision（Phase 0 的库只有 tasks）
    command.upgrade(cfg, "c26f1e0161a6")
    engine = create_engine(f"sqlite:///{db_file}")
    from sqlalchemy.orm import sessionmaker

    from app.models.task import Task, TaskStatus

    session = sessionmaker(bind=engine)()
    task = Task(title="legacy task", status=TaskStatus.CONFIRMED.value)
    session.add(task)
    session.commit()
    session.close()

    # 2) 升级到 head：Phase 1/3 的表全部出现，tasks 数据原样保留
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert EXPECTED_TABLES <= set(inspector.get_table_names())
    from sqlalchemy import text

    with engine.connect() as conn:
        title = conn.execute(text("SELECT title, status FROM tasks")).fetchone()
    assert title == ("legacy task", "CONFIRMED")


def test_migration_4b_roundtrip_preserves_4a_data(tmp_path):
    """4B 迁移往返：head 建库 → 4A 数据 → 索引 → downgrade 4B 结构移除且
    4A 数据保留 → re-upgrade → 结构恢复 → 重新索引成功。"""
    from sqlalchemy import event, text
    from sqlalchemy.orm import sessionmaker

    from app.core.database import _set_sqlite_pragma
    from app.embeddings.local_hash_provider import LocalHashEmbeddingProvider
    from app.models.document import (
        ChunkEmbedding,
        Document,
        DocumentChunk,
        DocumentIndexStatus,
        DocumentStatus,
    )
    from app.services.retrieval_index_service import RetrievalIndexService

    db_file = tmp_path / "4b.db"
    cfg = _make_config(db_file)

    # ---- 1) head（4B）：插入 4A 数据并建立索引 ----
    command.upgrade(cfg, "head")
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = SessionLocal()
    doc = Document(
        original_filename="4a.txt",
        content_type="text/plain",
        size_bytes=10,
        sha256="a" * 64,
        storage_key="f" * 32,
        status=DocumentStatus.READY.value,
        index_status=DocumentIndexStatus.NOT_INDEXED.value,
    )
    s.add(doc)
    s.commit()
    s.add(
        DocumentChunk(
            document_id=doc.id,
            chunk_index=0,
            content="JARVIS 迁移往返测试 提醒 周报",
            char_start=0,
            char_end=15,
            token_estimate=4,
        )
    )
    s.commit()
    doc_id = doc.id  # close 前保存；detached 实例访问属性会触发 expired 加载
    s.close()

    # 4B 结构验证：chunk_embeddings 唯一约束 + FK CASCADE + FTS 虚拟表 + 索引
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) >= EXPECTED_TABLES
    uniques = inspector.get_unique_constraints("chunk_embeddings")
    assert any(u["name"] == "uq_chunk_embeddings_chunk_id" for u in uniques)
    fks = inspector.get_foreign_keys("chunk_embeddings")
    assert any(
        fk["referred_table"] == "document_chunks"
        and fk.get("options", {}).get("ondelete") == "CASCADE"
        for fk in fks
    )
    assert "ix_documents_index_status" in set(inspector.get_indexes("documents")[i]["name"] for i in range(len(inspector.get_indexes("documents"))))
    with engine.connect() as c:
        fts = c.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='document_chunks_fts'"
            )
        ).fetchone()
        assert fts is not None, "FTS 虚拟表必须由迁移创建"

    # 用真实索引服务建索引（验证迁移后 schema 可承载 4B 功能）
    s = SessionLocal()
    RetrievalIndexService(s, provider=LocalHashEmbeddingProvider(dimension=384)).index_document(doc_id)
    assert s.query(ChunkEmbedding).count() == 1
    s.close()

    # ---- 2) downgrade 到 4A：4B 结构移除，4A 数据保留 ----
    command.downgrade(cfg, "8f2a4c1e9b37")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "chunk_embeddings" not in tables
    with engine.connect() as c:
        fts = c.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='document_chunks_fts'"
            )
        ).fetchone()
        assert fts is None, "downgrade 必须移除 FTS 虚拟表"
        cols = {r[1] for r in c.execute(text("PRAGMA table_info(documents)"))}
        assert "index_status" not in cols and "index_error_message" not in cols
        assert c.execute(text("SELECT COUNT(*) FROM documents")).scalar() == 1
        assert c.execute(text("SELECT COUNT(*) FROM document_chunks")).scalar() == 1

    # ---- 3) re-upgrade：结构恢复，4A 数据保留，重新索引成功 ----
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert set(inspector.get_table_names()) >= EXPECTED_TABLES
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM documents")).scalar() == 1
        assert c.execute(text("SELECT COUNT(*) FROM document_chunks")).scalar() == 1
        fts = c.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='document_chunks_fts'"
            )
        ).fetchone()
        assert fts is not None

    s = SessionLocal()
    stats = RetrievalIndexService(s, provider=LocalHashEmbeddingProvider(dimension=384)).index_document(doc_id)
    assert stats.embeddings_created == 1
    s.close()


def test_migration_4c_roundtrip_preserves_4ab_data(tmp_path):
    """4C 迁移往返：head（4C）建库 → 4A/4B 数据 + RAG 审计行 → downgrade 到
    4B revision（rag_answers 移除、4A/4B 数据保留）→ re-upgrade（结构恢复、
    数据保留、外键/索引正确）。"""
    from sqlalchemy import event, text
    from sqlalchemy.orm import sessionmaker

    from app.core.database import _set_sqlite_pragma
    from app.models.conversation import Conversation, Message, MessageRole
    from app.models.document import (
        ChunkEmbedding,
        Document,
        DocumentChunk,
        DocumentIndexStatus,
        DocumentStatus,
    )
    from app.models.rag_audit import RagAnswer, RagAnswerStatus

    db_file = tmp_path / "4c.db"
    cfg = _make_config(db_file)

    # ---- 1) head（4C）：4A/4B 数据 + RagAnswer 审计行 ----
    command.upgrade(cfg, "head")
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = SessionLocal()

    conv = Conversation(title="4c 对话")
    s.add(conv)
    s.commit()
    user_msg = Message(conversation_id=conv.id, role=MessageRole.USER.value, content="问题？")
    s.add(user_msg)
    s.commit()
    assistant_msg = Message(
        conversation_id=conv.id, role=MessageRole.ASSISTANT.value, content="答案"
    )
    s.add(assistant_msg)
    s.commit()
    conv_id = conv.id
    user_id = user_msg.id
    assistant_id = assistant_msg.id

    doc = Document(
        original_filename="4c.txt",
        content_type="text/plain",
        size_bytes=10,
        sha256="b" * 64,
        storage_key="e" * 32,
        status=DocumentStatus.READY.value,
        index_status=DocumentIndexStatus.INDEXED.value,
    )
    s.add(doc)
    s.commit()
    s.add(
        DocumentChunk(
            document_id=doc.id,
            chunk_index=0,
            content="4C 迁移往返 提醒 周报",
            char_start=0,
            char_end=12,
            token_estimate=4,
        )
    )
    s.commit()
    s.add(
        RagAnswer(
            conversation_id=conv_id,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            question="测试问题",
            language="auto",
            retrieval_mode="hybrid",
            document_ids_json="[1]",
            chunk_ids_json="[1]",
            citation_labels_json='["C1"]',
            status=RagAnswerStatus.ANSWERED.value,
            model="fake-model",
            prompt_tokens=10,
            completion_tokens=5,
            latency_ms=42,
            candidate_count=2,
            returned_count=1,
            context_chars=12,
            context_token_estimate=4,
        )
    )
    s.commit()
    s.close()

    inspector = inspect(engine)
    assert "rag_answers" in set(inspector.get_table_names())
    # 外键：conversation CASCADE、messages SET NULL
    fks = inspector.get_foreign_keys("rag_answers")
    fk_map = {
        tuple(fk["constrained_columns"]): (fk["referred_table"], fk.get("options", {}).get("ondelete"))
        for fk in fks
    }
    assert fk_map[("conversation_id",)] == ("conversations", "CASCADE")
    assert fk_map[("user_message_id",)] == ("messages", "SET NULL")
    assert fk_map[("assistant_message_id",)] == ("messages", "SET NULL")
    indexes = {idx["name"] for idx in inspector.get_indexes("rag_answers")}
    assert "ix_rag_answers_status" in indexes

    # ---- 2) downgrade 到 4B：rag_answers 移除，4A/4B 数据保留 ----
    command.downgrade(cfg, "4b0f2e1d9c83")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert "rag_answers" not in tables
    assert EXPECTED_TABLES - {"rag_answers"} <= tables
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM conversations")).scalar() == 1
        assert c.execute(text("SELECT COUNT(*) FROM messages")).scalar() == 2
        assert c.execute(text("SELECT COUNT(*) FROM documents")).scalar() == 1
        assert c.execute(text("SELECT COUNT(*) FROM chunk_embeddings")).scalar() == 0

    # ---- 3) re-upgrade：结构恢复，数据保留，外键仍正确 ----
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert EXPECTED_TABLES <= set(inspector.get_table_names())
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM conversations")).scalar() == 1
        assert c.execute(text("SELECT COUNT(*) FROM messages")).scalar() == 2
        assert c.execute(text("SELECT COUNT(*) FROM rag_answers")).scalar() == 0

    # 重新写入审计行验证可写性 + 外键真实生效（SET NULL 语义）
    s = SessionLocal()
    s.add(
        RagAnswer(
            conversation_id=conv_id,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            question="再测",
            language="zh",
            retrieval_mode="keyword",
            status=RagAnswerStatus.INSUFFICIENT_EVIDENCE.value,
            candidate_count=0,
            returned_count=0,
            context_chars=0,
            context_token_estimate=0,
        )
    )
    s.commit()
    s.close()
    # SET NULL 语义：删除消息后审计行保留、消息 id 置空
    with engine.connect() as c:
        c.execute(text("PRAGMA foreign_keys=ON"))
        c.execute(text("DELETE FROM messages WHERE id=:uid"), {"uid": user_id})
        c.commit()
        row = c.execute(text("SELECT user_message_id FROM rag_answers")).fetchone()
        assert row[0] is None, "消息删除后审计行 user_message_id 应为 NULL"


def test_downgrade_then_upgrade_roundtrip(tmp_path):
    """downgrade 可执行且可逆：head -> 001 -> head 后 schema 完整。"""
    db_file = tmp_path / "roundtrip.db"
    cfg = _make_config(db_file)

    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_file}")
    inspector = inspect(engine)
    assert EXPECTED_TABLES <= set(inspector.get_table_names())

    # downgrade（005 -> 002）：Phase 1/3 的表移除，tasks 保留
    command.downgrade(cfg, "4756c9756074")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"tasks", "conversations", "messages"} <= tables
    assert "task_proposals" not in tables
    assert "reminders" not in tables and "notifications" not in tables

    # downgrade 到 Phase 0 revision：新表移除，tasks 保留
    command.downgrade(cfg, "c26f1e0161a6")
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {"tasks", "alembic_version"} <= tables
    assert "conversations" not in tables
    assert "messages" not in tables

    # 再 upgrade：schema 完整恢复
    command.upgrade(cfg, "head")
    inspector = inspect(engine)
    assert EXPECTED_TABLES <= set(inspector.get_table_names())
    from app.models.conversation import Message, MessageRole

    from sqlalchemy.orm import sessionmaker

    session = sessionmaker(bind=engine)()
    msg = Message(conversation_id=1, role=MessageRole.USER.value, content="roundtrip")
    session.add(msg)
    session.commit()
    session.close()
