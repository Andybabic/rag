"""Per-usecase config and prompt overrides.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-08
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "usecase_config",
        sa.Column("use_case", sa.String(50), primary_key=True),
        sa.Column("chat_provider", sa.String(20)),
        sa.Column("embedding_provider", sa.String(20)),
        sa.Column("vision_provider", sa.String(20)),
        sa.Column("ollama_base_url", sa.Text),
        sa.Column("ollama_api_key_encrypted", sa.Text),
        sa.Column("openai_base_url", sa.Text),
        sa.Column("openai_api_key_encrypted", sa.Text),
        sa.Column("llm_model", sa.String(100)),
        sa.Column("embedding_model", sa.String(100)),
        sa.Column("vision_model", sa.String(100)),
        sa.Column("embedding_dimension", sa.Integer),
        sa.Column("temperature", sa.Float),
        sa.Column("max_tokens", sa.Integer),
        sa.Column("embed_batch_size", sa.Integer),
        sa.Column("agent_max_steps", sa.Integer),
        sa.Column("memory_max_chars", sa.Integer),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
    )

    op.create_table(
        "usecase_prompts",
        sa.Column("use_case", sa.String(50), nullable=False),
        sa.Column("prompt_key", sa.String(100), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("use_case", "prompt_key", name="pk_usecase_prompts"),
    )


def downgrade() -> None:
    op.drop_table("usecase_prompts")
    op.drop_table("usecase_config")
