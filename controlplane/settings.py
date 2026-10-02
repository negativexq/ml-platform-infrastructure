from __future__ import annotations

from typing import Literal

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
    # Logging. JSON by default (cluster log collectors want it); set false for a console.
    log_json: bool = True
    log_level: str = "INFO"

    # -- identity ------------------------------------------------------------------------
    # "oidc" (the default) requires a signed-in caller for every API request. "none" makes
    # everyone an anonymous platform admin: only for a laptop (`make cp-run` sets it).
    auth_mode: Literal["oidc", "none"] = "oidc"
    # The OpenID Connect issuer, e.g. https://keycloak.example.com/realms/mlp
    oidc_issuer: str = ""
    # The `aud` that API access tokens must carry (scripts, CI).
    oidc_audience: str = "mlp"
    # Browser sign-in: a confidential client of the issuer. Empty client id: API tokens only.
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_username_claim: str = "preferred_username"
    oidc_groups_claim: str = "groups"
    # Comma-separated subjects that may do anything anywhere, e.g. "group:platform-admins".
    platform_admins: str = ""
    # Signs the session cookie; at least 32 characters. Rotating it signs everyone out.
    session_secret: str = ""
    session_hours: float = 8.0
    # Where users reach the platform, for the sign-in redirect (https://mlp.example.com).
    # Empty: taken from the request, which is wrong behind some proxies.
    public_url: str = ""
