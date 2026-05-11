"""Per-usecase skills (rules) attached to the agent system prompt.

Revision ID: 0003
Revises: 0002
Create Date: 2026-05-11
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usecase_skills",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("use_case", sa.String(50), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("overview", sa.Text, nullable=False),
        sa.Column("detailed_task", sa.Text, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("idx_usecase_skills_use_case", "usecase_skills", ["use_case"])
    op.create_index(
        "uq_usecase_skills_name",
        "usecase_skills",
        ["use_case", "name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_usecase_skills_name", table_name="usecase_skills")
    op.drop_index("idx_usecase_skills_use_case", table_name="usecase_skills")
    op.drop_table("usecase_skills")
