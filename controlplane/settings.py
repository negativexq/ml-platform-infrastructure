from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CP_")

    database_role_enforcement: bool = True
    database_url: str = "postgresql+psycopg://platform:platform@localhost:5432/controlplane"
    # Empty: in-cluster config, then ~/.kube/config.
    kubeconfig: str = ""
    system_namespace: str = "mlp-system"
    api_service_account: str = "mlp-controlplane-api"
    project_secret_cluster_role: str = "mlp-system-mlp-controlplane-project-secrets"
    project_workload_cluster_role: str = "mlp-system-mlp-controlplane-project-workload-reader"
    reconciler_watchdog_seconds: float = Field(default=300, ge=10)
    platform_namespace: str = "ml-platform"
    observability_namespace: str = "observability"
    serving_namespaces: str = "knative-serving,kourier-system,istio-system"
    cluster_api_cidrs: str = ""
    external_https_cidrs: str = ""
    workflow_retention_seconds: int = Field(default=0, ge=0, le=31536000)
    project_egress_enabled: bool = False
    model_discovery_delay_seconds: int = Field(default=120, ge=0, le=86400)
    leader_election_enabled: bool = True
    leader_lease_name: str = "mlp-controlplane-reconciler"
    leader_lease_duration_seconds: int = Field(default=30, ge=10)
    leader_renew_deadline_seconds: int = Field(default=15, ge=5)
    leader_retry_seconds: float = Field(default=2, gt=0)
    reconcile_interval_seconds: float = 10.0
    # MLflow tracking server the control plane queries. Empty: tracking is not configured.
    mlflow_tracking_uri: str = ""
    # URI injected into pipeline steps (in-cluster address). Defaults to the one above.
    step_mlflow_tracking_uri: str = ""
    # Prometheus the rollout gates read per-revision metrics from. Empty: rollouts are not driven.
    prometheus_url: str = ""
    # Where outside callers reach the gateway (https://api.example.com). Shown in the UI and
    # used to build each public endpoint's URL. Empty: no gateway is deployed.
    gateway_url: str = ""
    job_image_digest_required: bool = True
    gateway_limit_store: Literal["postgres", "memory"] = "postgres"
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
    session_hours: float = Field(default=0.25, gt=0, le=0.25)
    # Where users reach the platform, for the sign-in redirect (https://mlp.example.com).
    # Empty: taken from the request, which is wrong behind some proxies.
    public_url: str = ""
