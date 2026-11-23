"""Add the durable intake idempotency table (issue #94).

Revision ID: 0003_durable_idempotency
Revises: 0002_auth_bootstrap
Create Date: 2026-11-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_durable_idempotency"
down_revision = "0002_auth_bootstrap"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "valence_idempotency",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "key"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_valence_idempotency_expires_at",
        "valence_idempotency",
        ["expires_at"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_valence_idempotency_expires_at",
        table_name="valence_idempotency",
        if_exists=True,
    )
    op.drop_table("valence_idempotency", if_exists=True)
