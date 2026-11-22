"""Add Instance, API key, and Integration credential tables (issue #57).

Revision ID: 0002_auth_bootstrap
Revises: 0001_initial
Create Date: 2026-11-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_auth_bootstrap"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "valence_instance",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("business_name", sa.String(), nullable=False),
        sa.Column("timezone", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_table(
        "valence_api_keys",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=True),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("salt", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("revoked_at", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_valence_api_keys_role",
        "valence_api_keys",
        ["role"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_valence_api_keys_prefix",
        "valence_api_keys",
        ["prefix"],
        if_not_exists=True,
    )
    op.create_table(
        "valence_integration_credentials",
        sa.Column("service", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("service"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("valence_integration_credentials", if_exists=True)
    op.drop_index("ix_valence_api_keys_prefix", table_name="valence_api_keys")
    op.drop_index("ix_valence_api_keys_role", table_name="valence_api_keys")
    op.drop_table("valence_api_keys", if_exists=True)
    op.drop_table("valence_instance", if_exists=True)
