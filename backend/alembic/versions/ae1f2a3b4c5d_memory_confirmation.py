"""Phase 10B: user_confirmed Memory source and minimal confirmation receipts.

1. personal_memories.source_type CHECK expands to ('manual','user_confirmed').
   SQLite cannot alter a CHECK, so the table is recreated with every row, ID,
   version, timestamp, the unique key, other CHECKs, the (category,key) index,
   AUTOINCREMENT and the sqlite_sequence high-water mark (never reset to MAX(id)).
   PostgreSQL drops/creates the named CHECK in place.
2. memory_confirmation_receipts: proposal_id PK, no key/category/value/content;
   memory_id has no foreign key so receipts survive Memory deletion.

Downgrade exists for disposable test databases only and refuses when any
user_confirmed Memory or any receipt exists (no silent conversion or discard).
"""

from alembic import context, op
import sqlalchemy as sa

revision = "ae1f2a3b4c5d"
down_revision = "9d0e1f2a3b4c"
branch_labels = None
depends_on = None

NEW_SOURCE_CHECK = "source_type IN ('manual','user_confirmed')"
OLD_SOURCE_CHECK = "source_type = 'manual'"
_COLUMNS = "id, category, key, value_json, source_type, version, created_at, updated_at"


def _memory_table(name: str, source_check: str) -> None:
    op.create_table(
        name,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("key", sa.String(80), nullable=False),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("key", name="uq_personal_memories_key"),
        sa.CheckConstraint("category IN ('preference','stable_fact','constraint')", name="ck_memory_category"),
        sa.CheckConstraint(source_check, name="ck_memory_source"),
        sa.CheckConstraint("version >= 1", name="ck_memory_version"),
        sqlite_autoincrement=True,
    )


def _sqlite_recreate_memories(source_check: str) -> None:
    bind = op.get_bind()
    has_sequence = bind.execute(sa.text(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")).scalar()
    high_water = None
    if has_sequence:
        high_water = bind.execute(sa.text(
            "SELECT seq FROM sqlite_sequence WHERE name='personal_memories'")).scalar()
    op.drop_index("ix_personal_memories_category_key", table_name="personal_memories")
    _memory_table("personal_memories__phase10b", source_check)
    op.execute(f"INSERT INTO personal_memories__phase10b ({_COLUMNS}) SELECT {_COLUMNS} FROM personal_memories")
    op.drop_table("personal_memories")
    op.rename_table("personal_memories__phase10b", "personal_memories")
    op.create_index("ix_personal_memories_category_key", "personal_memories", ["category", "key"])
    if high_water is not None:
        # Preserve the historical high-water mark so deleted IDs are never reused.
        current = bind.execute(sa.text(
            "SELECT seq FROM sqlite_sequence WHERE name='personal_memories'")).scalar()
        if current is None:
            bind.execute(sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('personal_memories', :seq)"),
                         {"seq": high_water})
        elif current < high_water:
            bind.execute(sa.text("UPDATE sqlite_sequence SET seq = :seq WHERE name='personal_memories'"),
                         {"seq": high_water})


def _replace_source_check(source_check: str) -> None:
    if op.get_context().dialect.name == "sqlite":
        _sqlite_recreate_memories(source_check)
    else:
        op.drop_constraint("ck_memory_source", "personal_memories", type_="check")
        op.create_check_constraint("ck_memory_source", "personal_memories", source_check)


def upgrade() -> None:
    _replace_source_check(NEW_SOURCE_CHECK)
    op.create_table(
        "memory_confirmation_receipts",
        sa.Column("proposal_id", sa.String(36), primary_key=True),
        sa.Column("operation", sa.String(10), nullable=False),
        sa.Column("memory_id", sa.Integer(), nullable=False),
        sa.Column("applied_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(proposal_id) = 36", name="ck_memory_receipt_proposal_id"),
        sa.CheckConstraint("operation IN ('CREATE','UPDATE')", name="ck_memory_receipt_operation"),
        sa.CheckConstraint("memory_id >= 1", name="ck_memory_receipt_memory_id"),
        sa.CheckConstraint("applied_version >= 1", name="ck_memory_receipt_version"),
        sa.CheckConstraint("status = 'APPLIED'", name="ck_memory_receipt_status"),
    )


def _refuse_if_phase10b_data() -> None:
    message = ("Refusing Phase 10B downgrade: user_confirmed memories or confirmation receipts exist. "
               "Downgrade is only for disposable test databases.")
    if op.get_context().dialect.name == "postgresql":
        # Works online and in offline SQL generation.
        op.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM personal_memories WHERE source_type = 'user_confirmed') "
            "OR EXISTS (SELECT 1 FROM memory_confirmation_receipts) THEN "
            f"RAISE EXCEPTION '{message}'; END IF; END $$;"
        )
        return
    if context.is_offline_mode():
        raise RuntimeError("Phase 10B downgrade requires an online connection to verify data")
    bind = op.get_bind()
    confirmed = bind.execute(sa.text(
        "SELECT COUNT(*) FROM personal_memories WHERE source_type = 'user_confirmed'")).scalar()
    receipts = bind.execute(sa.text("SELECT COUNT(*) FROM memory_confirmation_receipts")).scalar()
    if confirmed or receipts:
        raise RuntimeError(message)


def downgrade() -> None:
    _refuse_if_phase10b_data()
    op.drop_table("memory_confirmation_receipts")
    _replace_source_check(OLD_SOURCE_CHECK)
