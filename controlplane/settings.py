from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_")

    database_url: str = "postgresql+psycopg://platform:platform@localhost:5432/controlplane"
    # Empty: in-cluster config, then ~/.kube/config.
    kubeconfig: str = ""
    reconcile_interval_seconds: float = 10.0
    # MLflow tracking server the control plane queries. Empty: tracking is not configured.
    mlflow_tracking_uri: str = ""
    # URI injected into pipeline steps (in-cluster address). Defaults to the one above.
    step_mlflow_tracking_uri: str = ""
    # Prometheus the rollout gates read per-revision metrics from. Empty: rollouts are not driven.
    prometheus_url: str = ""
