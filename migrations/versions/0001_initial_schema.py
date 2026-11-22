"""Create the application tables (ADK owns its own session tables).

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "valence_orders",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("phone", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_valence_orders_phone",
        "valence_orders",
        ["phone"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_valence_orders_status",
        "valence_orders",
        ["status"],
        if_not_exists=True,
    )

    op.create_table(
        "valence_order_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_valence_order_events_order_id",
        "valence_order_events",
        ["order_id"],
        if_not_exists=True,
    )
    op.create_index(
        "ix_valence_order_events_created_at",
        "valence_order_events",
        ["created_at"],
        if_not_exists=True,
    )

    op.create_table(
        "valence_pending_approvals",
        sa.Column("approver_phone", sa.String(), nullable=False),
        sa.Column("order_ids", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("approver_phone"),
        if_not_exists=True,
    )

    op.create_table(
        "valence_catalog",
        sa.Column("collection", sa.String(), nullable=False),
        sa.Column("item_id", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("collection", "item_id"),
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_table("valence_catalog", if_exists=True)
    op.drop_table("valence_pending_approvals", if_exists=True)
    op.drop_index(
        "ix_valence_order_events_created_at",
        table_name="valence_order_events",
        if_exists=True,
    )
    op.drop_index(
        "ix_valence_order_events_order_id",
        table_name="valence_order_events",
        if_exists=True,
    )
    op.drop_table("valence_order_events", if_exists=True)
    op.drop_index(
        "ix_valence_orders_status", table_name="valence_orders", if_exists=True
    )
    op.drop_index(
        "ix_valence_orders_phone", table_name="valence_orders", if_exists=True
    )
    op.drop_table("valence_orders", if_exists=True)
