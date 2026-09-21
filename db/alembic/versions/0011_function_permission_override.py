"""Per-function permission override, set from the dashboard.

``ask_for_permission`` stays what the *source file* declares, derived at import
time from the ``ask for permission`` marker. ``permission_override`` is what an
admin decided in the UI, and it wins: the effective flag is
``COALESCE(permission_override, ask_for_permission)``.

Two columns rather than one, because saving a code file rewrites the function
rows from the source. With a single column, an admin who switched consent *on*
would lose that the next time anyone fixed a typo in the file — a security
setting disappearing silently is the one outcome worth extra schema for. NULL
means "follow the source", so nothing changes for skills nobody has touched.

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-15
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usecase_skill_functions",
        sa.Column("permission_override", sa.Boolean, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("usecase_skill_functions", "permission_override")
