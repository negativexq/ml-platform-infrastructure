from __future__ import annotations

from alembic import context

from controlplane.persistence.models import Base

config = context.config
target_metadata = Base.metadata

connection = config.attributes.get("connection")
if connection is None:
    raise RuntimeError("run migrations through controlplane.persistence.migrate")

context.configure(connection=connection, target_metadata=target_metadata)
with context.begin_transaction():
    context.run_migrations()
