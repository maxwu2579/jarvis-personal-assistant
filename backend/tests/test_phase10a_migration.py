"""Exactly one forward migration, disposable databases only."""

import io
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

import app.main as main
from app.core.config import settings
from app.models.personal_memory import PersonalMemory
from app.services.schema_lifecycle import DatabaseSchemaError, get_alembic_script_directory

OLD = '8c9d0e1f2a3b4'
NEW = '9d0e1f2a3b4c'
BACKEND = Path(__file__).resolve().parents[1]


def config(path):
    c = Config(str(BACKEND / 'alembic.ini'))
    c.set_main_option('script_location', str(BACKEND / 'alembic'))
    c.set_main_option('sqlalchemy.url', f'sqlite:///{path.as_posix()}')
    return c


def historical_snapshot(path):
    with sqlite3.connect(path) as db:
        return db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' AND tbl_name NOT IN ('personal_memories', 'memory_confirmation_receipts') ORDER BY type,name").fetchall()


def test_existing_upgrade_fail_closed_startup_and_downgrade(tmp_path, monkeypatch):
    path = tmp_path / 'migration.db'; c = config(path)
    command.upgrade(c, OLD)
    original = historical_snapshot(path)
    engine = sa.create_engine(f'sqlite:///{path.as_posix()}')
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(settings, 'database_url', str(engine.url))
    with pytest.raises(DatabaseSchemaError) as error:
        with TestClient(main.app):
            pass
    assert error.value.code == 'DATABASE_MIGRATION_NOT_CURRENT'
    assert historical_snapshot(path) == original
    # Phase 10A's "nothing else changed" invariant is evaluated at the Phase 10A revision itself;
    # later phases legitimately extend other tables (e.g. Phase 10C adds a messages column).
    command.upgrade(c, NEW)
    assert 'personal_memories' in sa.inspect(engine).get_table_names()
    assert historical_snapshot(path) == original
    command.upgrade(c, 'head')
    with TestClient(main.app) as client:
        assert client.get('/health').status_code == 200
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) >= {'personal_memories', 'tasks', 'executions'}
    assert {c['name'] for c in inspector.get_columns('personal_memories')} == {'id','category','key','value_json','source_type','version','created_at','updated_at'}
    assert inspector.get_unique_constraints('personal_memories')[0]['column_names'] == ['key']
    command.downgrade(c, OLD)
    assert 'personal_memories' not in sa.inspect(engine).get_table_names()
    assert historical_snapshot(path) == original
    command.upgrade(c, 'head')
    with TestClient(main.app) as client:
        assert client.get('/health').status_code == 200
    engine.dispose()


def test_fresh_migration_and_model_match(tmp_path, monkeypatch):
    path = tmp_path / 'fresh.db'
    command.upgrade(config(path), 'head')
    engine = sa.create_engine(f'sqlite:///{path.as_posix()}')
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(settings, 'database_url', str(engine.url))
    with TestClient(main.app):
        pass
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_heads() == (get_alembic_script_directory().get_current_head(),)
        from alembic.autogenerate import compare_metadata
        metadata = sa.MetaData()
        PersonalMemory.__table__.to_metadata(metadata)
        ctx = MigrationContext.configure(conn, opts={'include_object': lambda obj, name, kind, reflected, compared: name == 'personal_memories' if kind == 'table' else True})
        assert compare_metadata(ctx, metadata) == []
    engine.dispose()


def test_postgres_offline_migration_and_model_contract():
    script = get_alembic_script_directory()
    assert len(script.get_heads()) == 1  # Phase 10B extends the chain after NEW
    revision = script.get_revision(NEW)
    assert revision.down_revision == OLD
    output = io.StringIO()
    ctx = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql':True, 'output_buffer':output})
    with Operations.context(ctx):
        revision.module.upgrade()
        revision.module.downgrade()
    ddl = output.getvalue()
    assert 'CREATE TABLE personal_memories' in ddl and 'DROP TABLE personal_memories' in ddl
    assert 'JSON' in ddl and 'TIMESTAMP WITH TIME ZONE' in ddl
    assert 'CREATE TABLE personal_memories' in str(CreateTable(PersonalMemory.__table__).compile(dialect=postgresql.dialect()))
