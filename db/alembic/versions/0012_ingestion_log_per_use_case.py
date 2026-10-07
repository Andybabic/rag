"""Document identity per use case: UNIQUE (use_case, file_hash).

``file_hash`` used to be unique across the whole instance, so a byte-identical
file could belong to exactly one use case. That rules out cloning a use case
onto the same instance (the transfer import): every document of the clone
already "exists" under the original. Uniqueness now holds per use case — the
same file may be indexed by several use cases, each with its own stored copy.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-29
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both init.sql and 0001 declared the column UNIQUE inline, which Postgres
    # names ``<table>_<column>_key``.
    op.execute(
        "ALTER TABLE ingestion_log DROP CONSTRAINT IF EXISTS ingestion_log_file_hash_key"
    )
    op.create_unique_constraint(
        "uq_ingestion_log_use_case_file_hash", "ingestion_log", ["use_case", "file_hash"]
    )


def downgrade() -> None:
    # Fails if a hash is now shared by several use cases — deliberately: which
    # copy to drop is not a decision a migration should make silently.
    op.drop_constraint(
        "uq_ingestion_log_use_case_file_hash", "ingestion_log", type_="unique"
    )
    op.create_unique_constraint(
        "ingestion_log_file_hash_key", "ingestion_log", ["file_hash"]
    )
