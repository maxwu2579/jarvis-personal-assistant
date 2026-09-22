"""Read-only database schema lifecycle checks.

Alembic is the only production schema authority.  Application startup may
inspect the configured database and migration graph, but it must never create,
upgrade, stamp, or repair schema objects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from alembic.util.exc import CommandError
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError


BACKEND_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
ALEMBIC_SCRIPT_DIR = BACKEND_DIR / "alembic"

DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
DATABASE_FILE_MISSING = "DATABASE_FILE_MISSING"
DATABASE_SCHEMA_EMPTY = "DATABASE_SCHEMA_EMPTY"
DATABASE_MIGRATION_MISSING = "DATABASE_MIGRATION_MISSING"
DATABASE_MIGRATION_NOT_CURRENT = "DATABASE_MIGRATION_NOT_CURRENT"
DATABASE_MIGRATION_UNKNOWN_REVISION = "DATABASE_MIGRATION_UNKNOWN_REVISION"
DATABASE_MIGRATION_MULTIPLE_HEADS = "DATABASE_MIGRATION_MULTIPLE_HEADS"
DATABASE_MIGRATION_MULTIPLE_CURRENT = "DATABASE_MIGRATION_MULTIPLE_CURRENT"


class DatabaseSchemaError(RuntimeError):
    """Safe startup failure for an unavailable or incompatible schema."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        current_revisions: tuple[str, ...] = (),
        expected_revision: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.current_revisions = current_revisions
        self.expected_revision = expected_revision


@dataclass(frozen=True)
class SchemaReadiness:
    expected_revision: str
    current_revisions: tuple[str, ...]


def _alembic_config() -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_SCRIPT_DIR))
    return config


def get_alembic_script_directory() -> ScriptDirectory:
    """Load migration metadata without connecting to the application database."""
    return ScriptDirectory.from_config(_alembic_config())


def get_expected_alembic_head() -> str:
    """Return the sole migration head; multiple heads are never auto-merged."""
    heads = tuple(get_alembic_script_directory().get_heads())
    if len(heads) != 1:
        raise DatabaseSchemaError(
            DATABASE_MIGRATION_MULTIPLE_HEADS,
            "Alembic migration metadata must contain exactly one head; "
            "resolve multiple heads explicitly before starting JARVIS.",
            current_revisions=heads,
        )
    return heads[0]


def _configured_sqlite_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        return None
    if url.database == ":memory:":
        return None
    path = Path(url.database).expanduser()
    return path if path.is_absolute() else (Path.cwd() / path).resolve()


def _require_existing_sqlite_file(database_url: str) -> None:
    path = _configured_sqlite_path(database_url)
    if path is not None and not path.is_file():
        raise DatabaseSchemaError(
            DATABASE_FILE_MISSING,
            "Configured SQLite database file does not exist. Run the project's "
            "explicit `alembic upgrade head` workflow before starting JARVIS.",
        )


def verify_schema_connection(
    connection: Connection,
    *,
    expected_revision: str,
    script_directory: ScriptDirectory,
) -> SchemaReadiness:
    """Verify one live connection without changing schema or revision state."""
    current_revisions = tuple(MigrationContext.configure(connection).get_current_heads())

    if len(current_revisions) > 1:
        raise DatabaseSchemaError(
            DATABASE_MIGRATION_MULTIPLE_CURRENT,
            "Database has multiple current Alembic revisions and cannot be "
            "started safely.",
            current_revisions=current_revisions,
            expected_revision=expected_revision,
        )

    if not current_revisions:
        table_names = set(inspect(connection).get_table_names())
        non_version_tables = table_names - {"alembic_version"}
        if non_version_tables:
            raise DatabaseSchemaError(
                DATABASE_MIGRATION_MISSING,
                "Database contains schema objects but has no Alembic revision. "
                "Do not stamp it automatically; inspect and migrate it explicitly.",
                expected_revision=expected_revision,
            )
        raise DatabaseSchemaError(
            DATABASE_SCHEMA_EMPTY,
            "Database schema is empty. Run `alembic upgrade head` explicitly "
            "before starting JARVIS.",
            expected_revision=expected_revision,
        )

    current_revision = current_revisions[0]
    try:
        known_revision = script_directory.get_revision(current_revision)
    except (CommandError, ResolutionError):
        known_revision = None
    if known_revision is None:
        raise DatabaseSchemaError(
            DATABASE_MIGRATION_UNKNOWN_REVISION,
            "Database revision is not present in this project's Alembic migration "
            "graph; inspect the database before starting JARVIS.",
            current_revisions=current_revisions,
            expected_revision=expected_revision,
        )

    if current_revision != expected_revision:
        raise DatabaseSchemaError(
            DATABASE_MIGRATION_NOT_CURRENT,
            "Database schema is not at the required Alembic revision. Run the "
            "project's explicit migration command before starting JARVIS. "
            f"Current revision: {current_revision}; expected revision: "
            f"{expected_revision}.",
            current_revisions=current_revisions,
            expected_revision=expected_revision,
        )

    return SchemaReadiness(
        expected_revision=expected_revision,
        current_revisions=current_revisions,
    )


def verify_schema_ready(engine: Engine, database_url: str) -> SchemaReadiness:
    """Fail closed unless the configured database is at the sole Alembic head.

    The SQLite existence check intentionally happens before ``engine.connect`` so
    a readiness probe cannot create an unintended empty database file.
    """
    script_directory = get_alembic_script_directory()
    heads = tuple(script_directory.get_heads())
    if len(heads) != 1:
        raise DatabaseSchemaError(
            DATABASE_MIGRATION_MULTIPLE_HEADS,
            "Alembic migration metadata must contain exactly one head; "
            "resolve multiple heads explicitly before starting JARVIS.",
            current_revisions=heads,
        )
    expected_revision = heads[0]
    _require_existing_sqlite_file(database_url)

    try:
        with engine.connect() as connection:
            return verify_schema_connection(
                connection,
                expected_revision=expected_revision,
                script_directory=script_directory,
            )
    except DatabaseSchemaError:
        raise
    except SQLAlchemyError as exc:
        raise DatabaseSchemaError(
            DATABASE_UNAVAILABLE,
            "Database is unavailable; verify connectivity and run migrations "
            "explicitly before starting JARVIS.",
            expected_revision=expected_revision,
        ) from exc
