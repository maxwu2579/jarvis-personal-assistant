"""Phase 10C: Chat Memory-context provenance marker on messages.

Adds exactly one column, messages.memory_context_applied (Boolean, NOT NULL,
server default false). Additive DDL only: no table rebuild, no index, no
constraint, no Memory or receipt change. Existing rows, IDs, foreign keys,
indexes and sequences are preserved; every existing message becomes false
(accepted Phase 10B had no Chat Memory injection).

Downgrade exists for disposable test databases only. It refuses while any
message carries a true or invalid marker: dropping the column would re-enable
retransmission of answers generated with saved preferences. Nothing is cleared
or deleted silently.
"""

from alembic import context, op
import sqlalchemy as sa

revision = "b10c0d1e2f3a"
down_revision = "ae1f2a3b4c5d"
branch_labels = None
depends_on = None

REFUSAL = ("Refusing Phase 10C downgrade: messages with a true or invalid memory_context_applied "
           "marker exist. Downgrade is only for disposable test databases.")
# Only a legitimate stored false may be dropped. Equality to the dialect false, never
# SQL truthiness: malformed SQLite text/blob values must count as invalid.
_NOT_VALID_FALSE = "memory_context_applied IS NULL OR NOT (memory_context_applied = {false})"


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("memory_context_applied", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def _refuse_if_marked_or_invalid() -> None:
    if op.get_context().dialect.name == "postgresql":
        # Works online and in offline SQL generation.
        condition = _NOT_VALID_FALSE.format(false="false")
        op.execute(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM messages WHERE {condition}) THEN "
            f"RAISE EXCEPTION '{REFUSAL}'; END IF; END $$;"
        )
        return
    if context.is_offline_mode():
        raise RuntimeError("Phase 10C downgrade requires an online connection to verify data")
    blocked = op.get_bind().execute(
        sa.text(f"SELECT COUNT(*) FROM messages WHERE {_NOT_VALID_FALSE.format(false='0')}")
    ).scalar()
    if blocked:
        raise RuntimeError(REFUSAL)


def downgrade() -> None:
    _refuse_if_marked_or_invalid()
    op.drop_column("messages", "memory_context_applied")
