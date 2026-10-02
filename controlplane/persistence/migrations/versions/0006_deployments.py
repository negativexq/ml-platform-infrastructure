"""deployments: desired/active revision, revision model uri, endpoint status

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("deployments", sa.Column("status_reason", sa.Text(), nullable=True))
    op.add_column("deployments", sa.Column("desired_revision", sa.Integer(), nullable=True))
    op.add_column("deployments", sa.Column("active_revision", sa.Integer(), nullable=True))

    op.add_column(
        "deployment_revisions",
        sa.Column("model_uri", sa.Text(), nullable=False, server_default=""),
    )
    op.alter_column("deployment_revisions", "model_uri", server_default=None)

    op.add_column(
        "endpoints",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="PENDING"),
    )
    op.alter_column("endpoints", "status", server_default=None)
    op.add_column("endpoints", sa.Column("url", sa.Text(), nullable=True))
    op.add_column(
        "endpoints",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.alter_column("endpoints", "updated_at", server_default=None)


def downgrade() -> None:
    for column in ("updated_at", "url", "status"):
        op.drop_column("endpoints", column)
    op.drop_column("deployment_revisions", "model_uri")
    for column in ("active_revision", "desired_revision", "status_reason"):
        op.drop_column("deployments", column)
