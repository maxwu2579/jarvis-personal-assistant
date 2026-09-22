"""Phase 9.5: Alembic-only production schema lifecycle regression tests."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import app.main as main_module
from app.core.config import settings
from app.core.database import Base
from app.services import schema_lifecycle
from app.services.schema_lifecycle import (
    DATABASE_FILE_MISSING,
    DATABASE_MIGRATION_MISSING,
    DATABASE_MIGRATION_MULTIPLE_CURRENT,
    DATABASE_MIGRATION_MULTIPLE_HEADS,
    DATABASE_MIGRATION_NOT_CURRENT,
    DATABASE_MIGRATION_UNKNOWN_REVISION,
    DATABASE_SCHEMA_EMPTY,
    DatabaseSchemaError,
    get_expected_alembic_head,
    verify_schema_connection,
)


BACKEND_DIR = Path(__file__).resolve().parents[1]


def _url(db_file: Path) -> str:
    return f"sqlite:///{db_file.as_posix()}"


def _config(db_file: Path) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", _url(db_file))
    return config


def _migrate(db_file: Path, revision: str = "head") -> None:
    command.upgrade(_config(db_file), revision)


def _current_revisions(db_file: Path) -> tuple[str, ...]:
    engine = create_engine(_url(db_file))
    try:
        with engine.connect() as connection:
            return tuple(MigrationContext.configure(connection).get_current_heads())
    finally:
        engine.dispose()


def _sqlite_snapshot(db_file: Path) -> tuple[str, dict[str, int]]:
    connection = sqlite3.connect(
        f"file:{db_file.resolve().as_posix()}?mode=ro", uri=True
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        schema_rows = list(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            )
        )
        schema_payload = json.dumps(
            schema_rows, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if not row[0].startswith("document_chunks_fts")
        ]
        counts = {
            table: connection.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0]
            for table in tables
        }
        return hashlib.sha256(schema_payload).hexdigest(), counts
    finally:
        connection.close()


def _set_startup_database(monkeypatch, db_file: Path, engine) -> None:
    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(settings, "database_url", _url(db_file))


def _assert_startup_error(code: str) -> None:
    with pytest.raises(DatabaseSchemaError) as exc_info:
        with TestClient(main_module.app):
            pass
    assert exc_info.value.code == code


def test_fresh_alembic_database_starts_at_dynamic_head(tmp_path, monkeypatch):
    db_file = tmp_path / "fresh.db"
    assert not db_file.exists()

    _migrate(db_file)
    assert db_file.exists()
    assert _current_revisions(db_file) == (get_expected_alembic_head(),)

    engine = create_engine(_url(db_file))
    _set_startup_database(monkeypatch, db_file, engine)
    try:
        with TestClient(main_module.app) as client:
            assert client.get("/health").json() == {"status": "ok"}
    finally:
        engine.dispose()


def test_empty_database_fails_without_creating_business_tables(tmp_path, monkeypatch):
    db_file = tmp_path / "empty.db"
    sqlite3.connect(db_file).close()
    engine = create_engine(_url(db_file))
    _set_startup_database(monkeypatch, db_file, engine)
    try:
        _assert_startup_error(DATABASE_SCHEMA_EMPTY)
        with sqlite3.connect(db_file) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        assert tables == []
    finally:
        engine.dispose()


def test_behind_database_fails_without_auto_upgrade(tmp_path, monkeypatch):
    db_file = tmp_path / "behind.db"
    script_directory = schema_lifecycle.get_alembic_script_directory()
    head_revision = script_directory.get_revision(get_expected_alembic_head())
    parent_revision = head_revision.down_revision
    assert isinstance(parent_revision, str)
    _migrate(db_file, parent_revision)
    before = _current_revisions(db_file)

    engine = create_engine(_url(db_file))
    _set_startup_database(monkeypatch, db_file, engine)
    try:
        _assert_startup_error(DATABASE_MIGRATION_NOT_CURRENT)
    finally:
        engine.dispose()
    assert _current_revisions(db_file) == before == (parent_revision,)


def test_business_tables_without_revision_fail_closed(tmp_path, monkeypatch):
    db_file = tmp_path / "drifted.db"
    with sqlite3.connect(db_file) as connection:
        connection.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY)")

    engine = create_engine(_url(db_file))
    _set_startup_database(monkeypatch, db_file, engine)
    try:
        _assert_startup_error(DATABASE_MIGRATION_MISSING)
    finally:
        engine.dispose()

    with sqlite3.connect(db_file) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    assert tables == {"tasks"}


def test_unknown_revision_fails_closed(tmp_path, monkeypatch):
    db_file = tmp_path / "unknown.db"
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        connection.execute(
            "INSERT INTO alembic_version(version_num) VALUES ('unknown_revision')"
        )

    engine = create_engine(_url(db_file))
    _set_startup_database(monkeypatch, db_file, engine)
    try:
        _assert_startup_error(DATABASE_MIGRATION_UNKNOWN_REVISION)
    finally:
        engine.dispose()


def test_multiple_current_revisions_fail_closed(tmp_path, monkeypatch):
    db_file = tmp_path / "multiple_current.db"
    with sqlite3.connect(db_file) as connection:
        connection.execute(
            "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO alembic_version(version_num) VALUES (?)",
            [("revision_one",), ("revision_two",)],
        )

    engine = create_engine(_url(db_file))
    _set_startup_database(monkeypatch, db_file, engine)
    try:
        _assert_startup_error(DATABASE_MIGRATION_MULTIPLE_CURRENT)
    finally:
        engine.dispose()


def test_head_startup_preserves_schema_and_row_counts(tmp_path, monkeypatch):
    db_file = tmp_path / "head_unchanged.db"
    _migrate(db_file)
    before = _sqlite_snapshot(db_file)

    engine = create_engine(_url(db_file))
    _set_startup_database(monkeypatch, db_file, engine)
    try:
        with TestClient(main_module.app):
            pass
    finally:
        engine.dispose()

    assert _sqlite_snapshot(db_file) == before


def test_missing_sqlite_file_fails_before_connection(tmp_path, monkeypatch):
    db_file = tmp_path / "missing.db"
    engine = create_engine(_url(db_file))
    _set_startup_database(monkeypatch, db_file, engine)
    try:
        _assert_startup_error(DATABASE_FILE_MISSING)
        assert not db_file.exists()
    finally:
        engine.dispose()


def test_expected_head_is_derived_from_script_directory(monkeypatch):
    class FakeScriptDirectory:
        def get_heads(self):
            return ("future_dynamic_head",)

    monkeypatch.setattr(
        schema_lifecycle,
        "get_alembic_script_directory",
        lambda: FakeScriptDirectory(),
    )
    assert get_expected_alembic_head() == "future_dynamic_head"


def test_multiple_migration_heads_fail_closed(monkeypatch):
    class FakeScriptDirectory:
        def get_heads(self):
            return ("head_one", "head_two")

    monkeypatch.setattr(
        schema_lifecycle,
        "get_alembic_script_directory",
        lambda: FakeScriptDirectory(),
    )
    with pytest.raises(DatabaseSchemaError) as exc_info:
        get_expected_alembic_head()
    assert exc_info.value.code == DATABASE_MIGRATION_MULTIPLE_HEADS


def test_production_startup_never_calls_metadata_create_all(
    tmp_path, monkeypatch
):
    db_file = tmp_path / "no_create_all.db"
    _migrate(db_file)
    engine = create_engine(_url(db_file))
    _set_startup_database(monkeypatch, db_file, engine)

    def forbidden_create_all(*args, **kwargs):
        raise AssertionError("production startup called metadata.create_all")

    monkeypatch.setattr(Base.metadata, "create_all", forbidden_create_all)
    try:
        with TestClient(main_module.app) as client:
            assert client.get("/health").status_code == 200
    finally:
        engine.dispose()


def test_sqlite_health_rechecks_revision_after_startup(client, tmp_path, monkeypatch):
    db_file = tmp_path / "health_behind.db"
    script_directory = schema_lifecycle.get_alembic_script_directory()
    head_revision = script_directory.get_revision(get_expected_alembic_head())
    parent_revision = head_revision.down_revision
    assert isinstance(parent_revision, str)
    _migrate(db_file, parent_revision)

    engine = create_engine(_url(db_file))
    monkeypatch.setattr(main_module, "engine", engine)
    monkeypatch.setattr(settings, "database_url", _url(db_file))
    try:
        response = client.get("/health/db")
    finally:
        engine.dispose()

    assert response.status_code == 503
    assert response.json()["error"]["code"] == DATABASE_MIGRATION_NOT_CURRENT


def test_revision_checker_contract_is_dialect_neutral(monkeypatch):
    """The revision gate depends on Alembic/SQLAlchemy abstractions, not SQLite SQL."""

    class FakeMigrationContext:
        def get_current_heads(self):
            return ("dynamic_head",)

    class FakeScriptDirectory:
        def get_revision(self, revision):
            return object() if revision == "dynamic_head" else None

    monkeypatch.setattr(
        schema_lifecycle.MigrationContext,
        "configure",
        lambda connection: FakeMigrationContext(),
    )
    result = verify_schema_connection(
        object(),
        expected_revision="dynamic_head",
        script_directory=FakeScriptDirectory(),
    )
    assert result.current_revisions == ("dynamic_head",)
    assert result.expected_revision == "dynamic_head"
