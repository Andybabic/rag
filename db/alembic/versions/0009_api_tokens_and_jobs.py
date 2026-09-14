"""Public API: bearer tokens and asynchronous chat jobs.

Two tables back the externally reachable pipeline API (proxied by the
frontend, executed by the evaluation service):

``api_tokens``
    Bearer credentials issued in the dashboard. Only a SHA-256 hash is
    stored — the plaintext is shown once at creation and is unrecoverable
    afterwards. ``use_cases`` scopes a token to specific use cases (empty
    list = every use case), mirroring ``user_use_cases`` for humans.

``api_jobs``
    One row per API request: the question, its images, and the pipeline
    result. Durable on purpose — a caller polls with the request id, so the
    state has to survive a restart of any service. ``id`` doubles as the
    request id AND as the ``queries.id`` written when the job finishes, so a
    job, its feedback and its chat transcript all share one identifier.

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(100), nullable=False),
        # Leading characters of the plaintext, kept for recognition in the UI.
        sa.Column("token_prefix", sa.String(24), nullable=False),
        sa.Column("token_hash", sa.Text, nullable=False, unique=True),
        sa.Column(
            "use_cases",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("created_by", sa.String(100)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "api_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "token_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("api_tokens.id", ondelete="SET NULL"),
        ),
        sa.Column("use_case", sa.String(50), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(20), nullable=False, server_default="default"),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("query_text", sa.Text, nullable=False),
        sa.Column("images", postgresql.JSONB),
        sa.Column("history", postgresql.JSONB),
        sa.Column("config", postgresql.JSONB),
        sa.Column("result", postgresql.JSONB),
        sa.Column("error", sa.Text),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'done', 'error')",
            name="ck_api_jobs_status",
        ),
    )
    # The worker claims work with "oldest queued first"; without this index
    # that scan walks every finished job ever created.
    op.create_index(
        "idx_api_jobs_queued", "api_jobs", ["status", "created_at"]
    )
    op.create_index("idx_api_jobs_session", "api_jobs", ["session_id"])


def downgrade() -> None:
    op.drop_index("idx_api_jobs_session", table_name="api_jobs")
    op.drop_index("idx_api_jobs_queued", table_name="api_jobs")
    op.drop_table("api_jobs")
    op.drop_table("api_tokens")
