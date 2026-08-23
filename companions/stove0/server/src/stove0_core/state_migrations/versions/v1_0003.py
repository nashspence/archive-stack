"""Bound Stove0 operational retention and runnable-work scans."""

from datetime import UTC, datetime

from alembic import op
from sqlalchemy import Column, String, table

revision: str = "v1_0003"
down_revision: str | None = "v1_0002"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    now = datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    op.add_column(
        "stove0_lifecycle_events",
        Column("created_at", String(40), nullable=True),
    )
    events = table("stove0_lifecycle_events", Column("created_at", String(40)))
    op.execute(events.update().values(created_at=now))
    with op.batch_alter_table("stove0_lifecycle_events") as batch:
        batch.alter_column("created_at", existing_type=String(40), nullable=False)
        batch.create_index(
            "ix_stove0_lifecycle_events_created_at",
            ["created_at"],
            unique=False,
        )
    op.create_index(
        "ix_stove0_work_records_phase_work_id",
        "stove0_work_records",
        ["phase", "work_id"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError("stove0 state migrations are forward-only")
