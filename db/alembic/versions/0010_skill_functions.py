"""Plugin layer: executable skill functions, skill variables, execution audit.

Three changes, all additive so existing skills keep working untouched:

``usecase_skills`` gains ``code_language`` / ``code_file_name`` / ``code``
    A skill may carry exactly one code file (Python **or** JavaScript, never
    both). All three columns are nullable — a skill without a function part is
    unaffected.

``usecase_skills`` gains ``variables``
    A JSONB object of free-form name/value pairs per skill. Not exposed by the
    regular skill listing; the values leave the database only in the execution
    step and through the admin-only variables endpoint.

``usecase_skill_functions`` and ``skill_executions``
    The function table derived from a skill's source at save time (including
    the ``ask for permission`` flag and the per-function network allowlist),
    and a thin audit row per attempted execution.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-14
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("usecase_skills", sa.Column("code_language", sa.String(20)))
    op.add_column("usecase_skills", sa.Column("code_file_name", sa.String(255)))
    op.add_column("usecase_skills", sa.Column("code", sa.Text))
    op.add_column(
        "usecase_skills",
        sa.Column(
            "variables",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_usecase_skills_code_language",
        "usecase_skills",
        "code_language IS NULL OR code_language IN ('python', 'javascript')",
    )

    op.create_table(
        "usecase_skill_functions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "skill_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usecase_skills.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("signature", sa.Text, nullable=False, server_default=""),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "params",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "ask_for_permission",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "allowed_domains",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("timeout_ms", sa.Integer, nullable=False, server_default="10000"),
        sa.Column("memory_mb", sa.Integer, nullable=False, server_default="256"),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "idx_skill_functions_skill", "usecase_skill_functions", ["skill_id"]
    )
    op.create_index(
        "uq_skill_functions_name",
        "usecase_skill_functions",
        ["skill_id", "name"],
        unique=True,
    )

    op.create_table(
        "skill_executions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "skill_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usecase_skills.id", ondelete="SET NULL"),
        ),
        sa.Column("use_case", sa.String(50), nullable=False),
        sa.Column("skill_name", sa.String(100), nullable=False),
        sa.Column("function_name", sa.String(100), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True)),
        sa.Column("request_id", postgresql.UUID(as_uuid=True)),
        sa.Column("username", sa.String(100)),
        sa.Column(
            "args",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "permission_required",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("permission_granted", sa.Boolean),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("result_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'error', 'denied', 'timeout', 'unresolved')",
            name="ck_skill_executions_status",
        ),
    )
    op.create_index(
        "idx_skill_executions_use_case", "skill_executions", ["use_case", "created_at"]
    )
    op.create_index(
        "idx_skill_executions_session", "skill_executions", ["session_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_skill_executions_session", table_name="skill_executions")
    op.drop_index("idx_skill_executions_use_case", table_name="skill_executions")
    op.drop_table("skill_executions")

    op.drop_index("uq_skill_functions_name", table_name="usecase_skill_functions")
    op.drop_index("idx_skill_functions_skill", table_name="usecase_skill_functions")
    op.drop_table("usecase_skill_functions")

    op.drop_constraint(
        "ck_usecase_skills_code_language", "usecase_skills", type_="check"
    )
    op.drop_column("usecase_skills", "variables")
    op.drop_column("usecase_skills", "code")
    op.drop_column("usecase_skills", "code_file_name")
    op.drop_column("usecase_skills", "code_language")
