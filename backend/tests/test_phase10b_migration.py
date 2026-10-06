"""Phase 10B migration: exactly one forward revision, disposable databases only."""

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

import app.main as main
from app.core.config import settings
from app.models.memory_receipt import MemoryConfirmationReceipt
from app.models.personal_memory import PersonalMemory
from app.services.schema_lifecycle import DatabaseSchemaError, get_alembic_script_directory

OLD = '9d0e1f2a3b4c'
NEW = 'ae1f2a3b4c5d'
PHASE10C = 'b10c0d1e2f3a'  # current head; extends the chain after NEW
BACKEND = Path(__file__).resolve().parents[1]
ACCEPTED = {'c26f1e0161a6', '4756c9756074', '66405497a397', 'a1b2c3d4e5f6', 'fa94512e0868', '8f2a4c1e9b37',
            '4b0f2e1d9c83', '7c9d4e2f5a1b', 'e6f5d4c3b2a1', '6a7b8c9d0e1f2', '7b8c9d0e1f2a3', '8c9d0e1f2a3b4', OLD}
INSERT = ("INSERT INTO personal_memories(category,key,value_json,source_type,version,created_at,updated_at) "
          "VALUES(?,?,?,?,?,?,?)")


def config(path):
    c = Config(str(BACKEND / 'alembic.ini'))
    c.set_main_option('script_location', str(BACKEND / 'alembic'))
    c.set_main_option('sqlalchemy.url', f'sqlite:///{path.as_posix()}')
    return c


def other_objects(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' "
                          "AND tbl_name NOT IN ('personal_memories','memory_confirmation_receipts') "
                          "ORDER BY type,name").fetchall()


def memory_state(path):
    with sqlite3.connect(path) as db:
        return (db.execute('SELECT * FROM personal_memories ORDER BY id').fetchall(),
                db.execute("SELECT seq FROM sqlite_sequence WHERE name='personal_memories'").fetchall(),
                sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE tbl_name='personal_memories'")))


def seed_phase10a(path):
    with sqlite3.connect(path) as db:
        for i, key in enumerate(['alpha', 'beta', 'gamma', 'delta']):
            db.execute(INSERT, ('preference', key, f'"v{i}"', 'manual', i + 1, '2026-01-0%d 01:02:03' % (i + 1),
                                '2026-02-0%d 04:05:06' % (i + 1)))
        db.execute("DELETE FROM personal_memories WHERE key IN ('gamma','delta')")  # high-water 4, MAX(id) 2


def test_single_new_revision_and_no_historical_edits():
    script = get_alembic_script_directory()
    assert script.get_heads() == [PHASE10C]
    assert script.get_revision(PHASE10C).down_revision == NEW
    assert script.get_revision(NEW).down_revision == OLD
    assert {rev.revision for rev in script.walk_revisions()} == ACCEPTED | {NEW, PHASE10C}


def test_upgrade_from_phase10a_preserves_rows_ids_timestamps_and_high_water(tmp_path, monkeypatch):
    path = tmp_path / 'upgrade.db'
    c = config(path)
    command.upgrade(c, OLD)
    seed_phase10a(path)
    before_rows, before_seq, before_objects = memory_state(path)
    assert before_seq == [(4,)] and [r[0] for r in before_rows] == [1, 2]
    others = other_objects(path)
    # Startup fails closed on the Phase 10A revision; it never migrates.
    engine = sa.create_engine(f'sqlite:///{path.as_posix()}')
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(settings, 'database_url', str(engine.url))
    with pytest.raises(DatabaseSchemaError) as error:
        with TestClient(main.app):
            pass
    assert error.value.code == 'DATABASE_MIGRATION_NOT_CURRENT'
    assert memory_state(path)[0] == before_rows

    command.upgrade(c, NEW)
    rows, seq, objects = memory_state(path)
    assert rows == before_rows and seq == before_seq and objects == before_objects
    assert other_objects(path) == others  # the Phase 10B revision touches nothing else
    command.upgrade(c, 'head')  # later revisions (Phase 10C) leave Memory state untouched
    assert memory_state(path) == (before_rows, before_seq, before_objects)
    with TestClient(main.app) as client:
        assert client.get('/health/db').status_code == 200
    with sqlite3.connect(path) as db:
        db.execute(INSERT, ('preference', 'epsilon', '"x"', 'manual', 1, 'x', 'x'))
        assert db.execute("SELECT id FROM personal_memories WHERE key='epsilon'").fetchone() == (5,)  # never 3/4
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(INSERT, ('preference', 'bad', '"x"', 'llm_generated', 1, 'x', 'x'))
        db.execute(INSERT, ('preference', 'ok', '"x"', 'user_confirmed', 1, 'x', 'x'))
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(INSERT, ('preference', 'alpha', '"dup"', 'manual', 1, 'x', 'x'))
    engine.dispose()


def test_downgrade_refuses_phase10b_data_and_preserves_high_water(tmp_path):
    path = tmp_path / 'downgrade.db'
    c = config(path)
    command.upgrade(c, OLD)
    seed_phase10a(path)
    command.upgrade(c, 'head')
    with sqlite3.connect(path) as db:
        db.execute(INSERT, ('preference', 'confirmed', '"x"', 'user_confirmed', 1, 'x', 'x'))
    with pytest.raises(RuntimeError, match='Refusing Phase 10B downgrade'):
        command.downgrade(c, OLD)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM personal_memories WHERE key='confirmed'")
        db.execute("INSERT INTO memory_confirmation_receipts VALUES "
                   "('00000000-0000-4000-8000-000000000000','CREATE',9,1,'APPLIED','2026-01-01')")
    with pytest.raises(RuntimeError, match='Refusing Phase 10B downgrade'):
        command.downgrade(c, OLD)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT COUNT(*) FROM memory_confirmation_receipts').fetchone() == (1,)
        db.execute('DELETE FROM memory_confirmation_receipts')
    before = memory_state(path)
    command.downgrade(c, OLD)
    after = memory_state(path)
    assert after[0] == before[0] and after[1] == [(5,)] and after[2] == before[2]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='memory_confirmation_receipts'").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(INSERT, ('preference', 'again', '"x"', 'user_confirmed', 1, 'x', 'x'))


def test_fresh_migration_matches_models(tmp_path, monkeypatch):
    path = tmp_path / 'fresh.db'
    command.upgrade(config(path), 'head')
    engine = sa.create_engine(f'sqlite:///{path.as_posix()}')
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(settings, 'database_url', str(engine.url))
    with TestClient(main.app):
        pass
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_heads() == (PHASE10C,)
        metadata = sa.MetaData()
        PersonalMemory.__table__.to_metadata(metadata)
        MemoryConfirmationReceipt.__table__.to_metadata(metadata)
        names = {'personal_memories', 'memory_confirmation_receipts'}
        ctx = MigrationContext.configure(conn, opts={
            'include_object': lambda obj, name, kind, reflected, compared: name in names if kind == 'table' else True})
        assert compare_metadata(ctx, metadata) == []
    inspector = sa.inspect(engine)
    assert inspector.get_pk_constraint('memory_confirmation_receipts')['constrained_columns'] == ['proposal_id']
    assert inspector.get_foreign_keys('memory_confirmation_receipts') == []
    assert all(not c['nullable'] for c in inspector.get_columns('memory_confirmation_receipts'))
    engine.dispose()


def test_postgres_offline_ddl():
    revision = get_alembic_script_directory().get_revision(NEW)
    output = io.StringIO()
    ctx = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': output})
    with Operations.context(ctx):
        revision.module.upgrade()
        revision.module.downgrade()
    ddl = output.getvalue()
    assert 'ALTER TABLE personal_memories DROP CONSTRAINT ck_memory_source' in ddl
    assert "CHECK (source_type IN ('manual','user_confirmed'))" in ddl
    assert "CHECK (source_type = 'manual')" in ddl
    assert 'CREATE TABLE memory_confirmation_receipts' in ddl and 'TIMESTAMP WITH TIME ZONE' in ddl
    receipts_ddl = ddl.split('CREATE TABLE memory_confirmation_receipts')[1].split(';')[0]
    assert 'FOREIGN KEY' not in receipts_ddl and 'PRIMARY KEY (proposal_id)' in receipts_ddl
    assert 'RAISE EXCEPTION' in ddl and 'DROP TABLE memory_confirmation_receipts' in ddl
    assert 'DROP TABLE personal_memories' not in ddl  # PG alters in place; no data copy
