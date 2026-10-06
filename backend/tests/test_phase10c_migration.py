"""Phase 10C migration b10c0d1e2f3a: one additive Boolean on messages. Disposable databases only.

SQLite is executed for real. PostgreSQL is checked by offline DDL generation only; this is
not a live PostgreSQL pass.
"""

import hashlib
import io
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import app.main as main
from app.core.config import settings
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.conversation import Conversation, Message
from app.services.schema_lifecycle import (
    DatabaseSchemaError,
    get_alembic_script_directory,
    get_expected_alembic_head,
)

OLD = 'ae1f2a3b4c5d'
NEW = 'b10c0d1e2f3a'
BACKEND = Path(__file__).resolve().parents[1]
REFUSAL = 'Refusing Phase 10C downgrade'


def config(path):
    c = Config(str(BACKEND / 'alembic.ini'))
    c.set_main_option('script_location', str(BACKEND / 'alembic'))
    c.set_main_option('sqlalchemy.url', f'sqlite:///{path.as_posix()}')
    return c


def q(path, sql, *args):
    with sqlite3.connect(path) as db:
        return db.execute(sql, args).fetchall()


def schema_objects(path, *, messages):
    op = '=' if messages else '!='
    return q(path, "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                   f"AND tbl_name {op} 'messages' ORDER BY type,name")


def seed_phase10b(path):
    """A populated ae1f2a3b4c5d database: conversations, messages (with gaps), dependants, Memory, receipts."""
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(1,'c1','2026-01-01','2026-01-02')")
        db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(2,NULL,'2026-01-03','2026-01-04')")
        for mid, cid, role, content in ((1, 1, 'USER', 'hello'), (2, 1, 'ASSISTANT', 'hi there'),
                                        (3, 1, 'USER', 'deleted later'), (7, 2, 'SYSTEM', 'note'),
                                        (8, 2, 'ASSISTANT', 'reply')):
            db.execute('INSERT INTO messages(id,conversation_id,role,content,model,prompt_tokens,completion_tokens,'
                       'latency_ms,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                       (mid, cid, role, content, 'm' if role == 'ASSISTANT' else None, mid, mid * 2, mid * 3,
                        f'2026-02-0{mid} 01:02:03.000000'))
        db.execute('DELETE FROM messages WHERE id = 3')  # sequence high-water (8) is above the live rows
        db.execute("INSERT INTO task_proposals(conversation_id,user_message_id,assistant_message_id,action,status,"
                   "created_at) VALUES(1,1,2,'create_task_draft','REJECTED','2026-02-01')")
        db.execute("INSERT INTO personal_memories(category,key,value_json,source_type,version,created_at,updated_at) "
                   "VALUES('preference','preferred_study_time','\"evening\"','user_confirmed',3,'2026-01-01','2026-01-05')")
        db.execute("INSERT INTO memory_confirmation_receipts VALUES "
                   "('00000000-0000-4000-8000-000000000001','CREATE',1,1,'APPLIED','2026-01-01')")


OLD_COLUMNS = 'id,conversation_id,role,content,model,prompt_tokens,completion_tokens,latency_ms,created_at'


def preserved_state(path):
    return {
        'messages': q(path, f'SELECT {OLD_COLUMNS} FROM messages ORDER BY id'),
        'conversations': q(path, 'SELECT * FROM conversations ORDER BY id'),
        'task_proposals': q(path, 'SELECT * FROM task_proposals ORDER BY id'),
        'memories': q(path, 'SELECT * FROM personal_memories ORDER BY id'),
        'receipts': q(path, 'SELECT * FROM memory_confirmation_receipts'),
        'sequences': q(path, 'SELECT name, seq FROM sqlite_sequence ORDER BY name'),
        'message_fks': q(path, 'PRAGMA foreign_key_list(messages)'),
        'message_indexes': q(path, "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='messages' "
                                   "ORDER BY name"),
        'other_objects': schema_objects(path, messages=False),
    }


def start_app(path, monkeypatch):
    engine = sa.create_engine(f'sqlite:///{path.as_posix()}')
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(settings, 'database_url', str(engine.url))
    return engine


# ------------------------------------------------------------------ chain / head

def test_single_new_revision_extends_the_chain():
    script = get_alembic_script_directory()
    assert script.get_heads() == [NEW] and get_expected_alembic_head() == NEW
    assert script.get_revision(NEW).down_revision == OLD
    revisions = [rev.revision for rev in script.walk_revisions()]
    assert revisions[0] == NEW and revisions[1] == OLD and len(revisions) == len(set(revisions)) == 15
    source = (BACKEND / 'alembic' / 'versions' / f'{NEW}_chat_memory_provenance.py').read_text(encoding='utf-8')
    assert source.count('op.add_column') == 1 and 'create_table' not in source and 'create_index' not in source
    assert 'personal_memories' not in source.split('def upgrade')[1]


def test_fresh_chain_matches_the_model_and_starts(tmp_path, monkeypatch):
    path = tmp_path / 'fresh.db'
    command.upgrade(config(path), 'head')
    engine = start_app(path, monkeypatch)
    with TestClient(main.app) as client:
        assert client.get('/health/db').status_code == 200
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_heads() == (NEW,)
        metadata = sa.MetaData()
        Conversation.__table__.to_metadata(metadata)
        Message.__table__.to_metadata(metadata)
        ctx = MigrationContext.configure(conn, opts={
            'compare_server_default': True,
            'include_object': lambda obj, name, kind, reflected, compared: name in ('messages', 'conversations')
            if kind == 'table' else True})
        assert compare_metadata(ctx, metadata) == []
    columns = {c['name']: c for c in sa.inspect(engine).get_columns('messages')}
    marker = columns['memory_context_applied']
    assert marker['nullable'] is False and str(marker['default']).strip("'()") in ('0', 'false')
    assert isinstance(marker['type'], sa.Boolean)
    assert [name for name in columns if 'memory' in name] == ['memory_context_applied']  # exactly one column
    engine.dispose()


# ------------------------------------------------------------------ upgrade of a populated 10B database

def test_upgrade_preserves_everything_and_backfills_false(tmp_path, monkeypatch):
    path = tmp_path / 'upgrade.db'
    c = config(path)
    command.upgrade(c, OLD)
    seed_phase10b(path)
    before = preserved_state(path)
    assert ('messages', 8) in before['sequences'] and [r[0] for r in before['messages']] == [1, 2, 7, 8]
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    # New code against the old schema fails readiness; it neither migrates nor touches the file.
    engine = start_app(path, monkeypatch)
    with pytest.raises(DatabaseSchemaError) as error:
        with TestClient(main.app):
            pass
    assert error.value.code == 'DATABASE_MIGRATION_NOT_CURRENT'
    assert error.value.current_revisions == (OLD,) and error.value.expected_revision == NEW
    engine.dispose()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert 'memory_context_applied' not in [r[1] for r in q(path, 'PRAGMA table_info(messages)')]

    command.upgrade(c, 'head')
    assert preserved_state(path) == before  # rows, IDs, FKs, indexes, sequences, Memory, receipts, other DDL
    assert q(path, 'SELECT id, memory_context_applied, typeof(memory_context_applied) FROM messages ORDER BY id') == [
        (1, 0, 'integer'), (2, 0, 'integer'), (7, 0, 'integer'), (8, 0, 'integer')]  # every old row is false
    info = {r[1]: r for r in q(path, 'PRAGMA table_info(messages)')}
    assert list(info) == OLD_COLUMNS.split(',') + ['memory_context_applied']
    assert info['memory_context_applied'][3] == 1 and info['memory_context_applied'][4] in ('0', "'0'")  # NOT NULL, default

    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(1,'USER','new','2026-03-01')")
        assert db.execute('SELECT id, memory_context_applied FROM messages ORDER BY id DESC LIMIT 1').fetchone() == (9, 0)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO messages(conversation_id,role,content,created_at,memory_context_applied) "
                       "VALUES(1,'USER','null','2026-03-01',NULL)")
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')  # must be set outside a transaction
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO messages(conversation_id,role,content,created_at) VALUES(99,'USER','x','2026-03-01')")
        db.execute('DELETE FROM conversations WHERE id = 2')  # ON DELETE CASCADE still works
        assert db.execute('SELECT COUNT(*) FROM messages WHERE conversation_id = 2').fetchone() == (0,)

    engine = start_app(path, monkeypatch)

    def migrated_db():
        with Session(engine) as session:
            yield session

    main.app.dependency_overrides[get_db] = migrated_db
    try:
        with TestClient(main.app) as client:
            assert client.get('/health/db').status_code == 200
            messages = client.get('/api/conversations/1/messages').json()
            assert [m['memory_context_applied'] for m in messages] == [False, False, False]
            assert [m['content'] for m in messages] == ['hello', 'hi there', 'new']
        with Session(engine) as session:  # every pre-existing row is provably a legitimate false
            assert [m.memory_context_storage_valid_false for m in session.query(Message).order_by(Message.id)] == [True] * 3
    finally:
        main.app.dependency_overrides.clear()
        engine.dispose()


# ------------------------------------------------------------------ downgrade guard

@pytest.mark.parametrize('label, raw', [('true', 1), ('empty text', ''), ('empty blob', b''), ('text', 'x'),
                                        ('int 2', 2), ('real', 0.5)])
def test_downgrade_refuses_true_or_invalid_provenance(tmp_path, label, raw):
    path = tmp_path / 'guard.db'
    c = config(path)
    command.upgrade(c, OLD)
    seed_phase10b(path)
    command.upgrade(c, 'head')
    with sqlite3.connect(path) as db:
        db.execute('UPDATE messages SET memory_context_applied = ? WHERE id = 2', (raw,))
    before = q(path, 'SELECT id, content, typeof(memory_context_applied) FROM messages ORDER BY id')
    with pytest.raises(RuntimeError, match=REFUSAL) as error:
        command.downgrade(c, OLD)
    assert 'hi there' not in str(error.value)  # fixed safe message, no row content
    # Nothing was dropped, cleared or deleted.
    assert q(path, 'SELECT id, content, typeof(memory_context_applied) FROM messages ORDER BY id') == before
    with sa.create_engine(f'sqlite:///{path.as_posix()}').connect() as conn:
        assert MigrationContext.configure(conn).get_current_heads() == (NEW,)


def test_safe_downgrade_removes_only_the_column_and_reupgrade_restores_it(tmp_path, monkeypatch):
    path = tmp_path / 'roundtrip.db'
    c = config(path)
    command.upgrade(c, OLD)
    seed_phase10b(path)
    original = preserved_state(path)
    original_messages_ddl = schema_objects(path, messages=True)
    command.upgrade(c, 'head')
    command.downgrade(c, OLD)  # every marker is a legitimate false
    assert preserved_state(path) == original
    assert 'memory_context_applied' not in [r[1] for r in q(path, 'PRAGMA table_info(messages)')]
    assert [row[:3] for row in schema_objects(path, messages=True)] == [row[:3] for row in original_messages_ddl]
    command.upgrade(c, 'head')
    assert preserved_state(path) == original
    assert q(path, 'SELECT DISTINCT memory_context_applied FROM messages') == [(0,)]
    engine = start_app(path, monkeypatch)
    with TestClient(main.app) as client:
        assert client.get('/health/db').status_code == 200
    engine.dispose()


def test_startup_and_health_fail_closed_after_downgrade(tmp_path, monkeypatch):
    path = tmp_path / 'old.db'
    c = config(path)
    command.upgrade(c, 'head')
    engine = start_app(path, monkeypatch)
    with TestClient(main.app) as client:
        assert client.get('/health/db').status_code == 200
        command.downgrade(c, OLD)
        response = client.get('/health/db')  # readiness re-checks the revision; no auto-repair
        assert response.status_code == 503 and response.json()['error']['code'] == 'DATABASE_MIGRATION_NOT_CURRENT'
    assert 'memory_context_applied' not in [r[1] for r in q(path, 'PRAGMA table_info(messages)')]
    engine.dispose()


# ------------------------------------------------------------------ PostgreSQL (offline DDL only)

def test_postgres_offline_ddl_and_model_contract():
    revision = get_alembic_script_directory().get_revision(NEW)
    output = io.StringIO()
    ctx = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    with Operations.context(ctx):
        revision.module.upgrade()
        revision.module.downgrade()
    ddl = output.getvalue()
    assert 'ALTER TABLE messages ADD COLUMN memory_context_applied BOOLEAN DEFAULT false NOT NULL' in ddl
    assert ddl.count('ADD COLUMN') == 1 and 'CREATE TABLE' not in ddl and 'CREATE INDEX' not in ddl
    guard, drop = ddl.split('ALTER TABLE messages DROP COLUMN memory_context_applied')
    assert 'RAISE EXCEPTION' in guard and REFUSAL in guard
    assert 'memory_context_applied IS NULL OR NOT (memory_context_applied = false)' in guard
    assert 'IS FALSE' not in ddl.upper() and 'personal_memories' not in ddl and 'memory_confirmation_receipts' not in ddl
    model_ddl = str(CreateTable(Message.__table__).compile(dialect=postgresql.dialect()))
    assert 'memory_context_applied BOOLEAN DEFAULT false NOT NULL' in model_ddl
    assert 'memory_context_storage_valid_false' not in model_ddl  # derived property is not a column
    select_sql = str(sa.select(Message).compile(dialect=postgresql.dialect()))
    assert 'messages.memory_context_applied = false AS' in select_sql

