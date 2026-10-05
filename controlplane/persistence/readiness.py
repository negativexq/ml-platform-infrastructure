"""Read-only database/schema checks for production HTTP probes."""

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text

from controlplane.persistence.migrate import SCRIPT_LOCATION


class DatabaseReadiness:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        config = Config()
        config.set_main_option("script_location", str(SCRIPT_LOCATION))
        self._heads = set(ScriptDirectory.from_config(config).get_heads())

    def __call__(self) -> None:
        with self._engine.connect() as connection:
            if connection.dialect.name == "postgresql":
                connection.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            connection.execute(text("SELECT 1"))
            current: set[str] = set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            )
            if current != self._heads:
                raise RuntimeError("database migrations do not match this release")
