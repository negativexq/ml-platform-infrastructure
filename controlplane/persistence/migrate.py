"""Run Alembic migrations programmatically (no alembic.ini)."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text

from controlplane.persistence.sql import make_engine

SCRIPT_LOCATION = Path(__file__).parent / "migrations"


def _config(connection: object) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    cfg.attributes["connection"] = connection
    return cfg


# Stable identifier shared by every release and both upgrade/downgrade paths.
MIGRATION_LOCK = 0x4D4C504D494752


def _lock(connection: Connection) -> None:
    if connection.dialect.name == "postgresql":
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK}
        )
        if not acquired:
            raise RuntimeError("another control-plane migration owns the database lock")


def upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as conn:
        _lock(conn)
        command.upgrade(_config(conn), revision)


def downgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as conn:
        _lock(conn)
        command.downgrade(_config(conn), revision)


def main(argv: list[str]) -> int:
    from controlplane.settings import Settings

    engine = make_engine(Settings().database_url)
    if argv[:1] == ["upgrade"]:
        upgrade(engine, argv[1] if len(argv) > 1 else "head")
        return 0
    print("usage: python -m controlplane.persistence.migrate upgrade [revision]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
