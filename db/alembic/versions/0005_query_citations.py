"""Persist mapped citations alongside each query.

Lets the history view reload a full conversation with its source citations,
not just the answer text and intermediate steps.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("queries", sa.Column("citations", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("queries", "citations")
