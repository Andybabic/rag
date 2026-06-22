"""Persist user-attached images alongside each query.

Lets the history view reload a conversation with the images the user sent
(data URIs). Note: this can grow the row size — see the README caveat.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("queries", sa.Column("images", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("queries", "images")
