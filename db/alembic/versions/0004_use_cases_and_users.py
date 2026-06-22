"""DB-backed use-case registry and user accounts.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-22
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "use_cases",
        sa.Column("id", sa.String(50), primary_key=True),
        sa.Column("slug", sa.String(50), nullable=False, unique=True),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("color", sa.String(40), server_default=""),
        sa.Column("accent", sa.String(20), server_default=""),
        sa.Column("roles", postgresql.JSONB, nullable=False),
        sa.Column("agent_action_names", postgresql.JSONB, nullable=False),
        sa.Column("default_collection", sa.String(100), nullable=False),
        sa.Column("collection_prefixes", postgresql.JSONB),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("username", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text, nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="user"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
    )


def downgrade() -> None:
    op.drop_table("users")
    op.drop_table("use_cases")
