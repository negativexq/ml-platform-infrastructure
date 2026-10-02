from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_")

    database_url: str = "postgresql+psycopg://platform:platform@localhost:5432/controlplane"
    # Empty: in-cluster config, then ~/.kube/config.
    kubeconfig: str = ""
    reconcile_interval_seconds: float = 10.0
