"""Phase 10A: manual personal memory, no history or operational references."""

from alembic import op
import sqlalchemy as sa

revision = "9d0e1f2a3b4c"
down_revision = "8c9d0e1f2a3b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "personal_memories",
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
        sa.CheckConstraint("source_type = 'manual'", name="ck_memory_source"),
        sa.CheckConstraint("version >= 1", name="ck_memory_version"),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_personal_memories_category_key", "personal_memories", ["category", "key"])


def downgrade() -> None:
    op.drop_index("ix_personal_memories_category_key", table_name="personal_memories")
    op.drop_table("personal_memories")
