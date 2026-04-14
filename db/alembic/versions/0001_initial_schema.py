"""Initial schema – queries, feedback, ingestion_log, agent_memory, evaluation_runs.

Revision ID: 0001
Revises: None
Create Date: 2026-03-17
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- queries
    op.create_table(
        "queries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("use_case", sa.String(50), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True)),
        sa.Column("role", sa.String(20), server_default="default"),
        sa.Column("query_text", sa.Text, nullable=False),
        sa.Column("answer_text", sa.Text),
        sa.Column("chunks_used", postgresql.JSONB),
        sa.Column("agent_steps", postgresql.JSONB),
        sa.Column("scores", postgresql.JSONB),
        sa.Column("sufficient", sa.Boolean),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()")),
    )
    op.create_index("idx_queries_use_case", "queries", ["use_case"])
    op.create_index("idx_queries_created_at", "queries", ["created_at"])

    # -- feedback
    op.create_table(
        "feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("query_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("queries.id")),
        sa.Column("rating", sa.String(10)),
        sa.Column("comment", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()")),
        sa.CheckConstraint(
            "rating IN ('positive', 'negative')",
            name="ck_feedback_rating",
        ),
    )
    op.create_index("idx_feedback_query_id", "feedback", ["query_id"])

    # -- ingestion_log
    op.create_table(
        "ingestion_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("file_name", sa.String(500), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("collection", sa.String(100)),
        sa.Column("use_case", sa.String(50)),
        sa.Column("chunk_count", sa.Integer),
        sa.Column("status", sa.String(20), server_default="ok"),
        sa.Column("error_detail", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()")),
    )
    op.create_index("idx_ingestion_file_hash", "ingestion_log", ["file_hash"])

    # -- agent_memory
    op.create_table(
        "agent_memory",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("use_case", sa.String(50), nullable=False, unique=True),
        sa.Column("memory_text", sa.Text),
        sa.Column("version", sa.Integer, server_default="1"),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()")),
    )

    # -- evaluation_runs
    op.create_table(
        "evaluation_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("query_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("queries.id")),
        sa.Column("reranked_chunks", postgresql.JSONB),
        sa.Column("threshold_passed", sa.Boolean),
        sa.Column("refined_queries", postgresql.JSONB),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    op.drop_table("evaluation_runs")
    op.drop_table("agent_memory")
    op.drop_table("ingestion_log")
    op.drop_table("feedback")
    op.drop_index("idx_queries_created_at", table_name="queries")
    op.drop_index("idx_queries_use_case", table_name="queries")
    op.drop_table("queries")
