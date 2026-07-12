"""partial index on unpublished events

Revision ID: e49a588aca48
Revises: dbc8cc0f1165
Create Date: 2026-07-12 21:24:04.590388

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e49a588aca48'
down_revision: str | Sequence[str] | None = 'dbc8cc0f1165'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Outbox relay polls WHERE published_at IS NULL constantly; partial index
    keeps that scan O(pending) instead of O(all events ever)."""
    op.create_index(
        "ix_events_unpublished",
        "events",
        ["house_id", "seq"],
        postgresql_where=sa.text("published_at IS NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_events_unpublished", table_name="events")
