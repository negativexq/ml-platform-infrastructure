"""Run Alembic migrations programmatically (no alembic.ini)."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine

from controlplane.persistence.sql import make_engine

SCRIPT_LOCATION = Path(__file__).parent / "migrations"


def _config(connection: object) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    cfg.attributes["connection"] = connection
    return cfg


def upgrade(engine: Engine, revision: str = "head") -> None:
    with engine.begin() as conn:
        command.upgrade(_config(conn), revision)


def downgrade(engine: Engine, revision: str) -> None:
    with engine.begin() as conn:
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
