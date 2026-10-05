"""Reserve a model version for at most one active rollout."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing duplicate reservations require an operator to abort/complete rollouts first.
    # Do not silently mark them finished while they may still serve canary traffic.
    op.create_index(
        "uq_rollouts_one_active_version",
        "rollouts",
        ["model_version_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING', 'PROGRESSING')"),
    )


def downgrade() -> None:
    op.drop_index("uq_rollouts_one_active_version", table_name="rollouts")
