"""Assign users to use cases (many-to-many access control).

Lets an admin restrict which use cases a (non-admin) user may interact with.
An empty assignment means the user has access to no use cases; admins always
have access to everything (enforced in the frontend layer, not the schema).

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_use_cases",
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "use_case",
            sa.String(50),
            sa.ForeignKey("use_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.PrimaryKeyConstraint("user_id", "use_case", name="pk_user_use_cases"),
    )
    op.create_index("idx_user_use_cases_user", "user_use_cases", ["user_id"])


def downgrade() -> None:
    op.drop_index("idx_user_use_cases_user", table_name="user_use_cases")
    op.drop_table("user_use_cases")
