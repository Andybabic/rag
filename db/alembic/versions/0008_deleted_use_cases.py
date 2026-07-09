"""Tombstone deleted use cases so the boot-time seed never resurrects them.

The evaluation service seeds default use cases from ``config/use_cases.json`` on
every startup (create-if-missing). Without a record of deletions, any default a
user deleted via the dashboard reappeared on the next restart. This table keeps
a tombstone per deleted id; the seed skips tombstoned ids, and re-creating a use
case with the same id clears its tombstone.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-09
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "deleted_use_cases",
        sa.Column("use_case", sa.String(50), primary_key=True),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("deleted_use_cases")
