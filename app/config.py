"""Application configuration, sourced from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ML_", env_file=".env", extra="ignore")

    app_name: str = "ml-platform-inference"
    log_level: str = "INFO"
    log_json: bool = True

    # Model source. When `model_uri` is set the artifact is resolved through
    # MLflow (M1+); otherwise the local file at `model_path` is loaded (M0).
    model_path: str = "artifacts/model.joblib"
    model_uri: str | None = None
    mlflow_tracking_uri: str | None = None

    # S3/MinIO endpoint used by MLflow's artifact client.
    mlflow_s3_endpoint_url: str | None = None

    # Bounded startup behaviour for model loading.
    model_load_max_retries: int = 5
    model_load_retry_seconds: float = 2.0
    # After the initial burst fails, retry in the background at this interval
    # so the service recovers without a restart. 0 disables the recheck loop.
    model_load_recheck_seconds: float = 30.0

    # Fault injection for failure drills (M5). Adds artificial latency to
    # every prediction; 0 disables it. Never set outside a drill.
    predict_fault_latency_ms: int = 0

    # Fail each MLflow HTTP call fast; our own loop owns the retry policy.
    mlflow_http_request_max_retries: int = 1
    mlflow_http_request_timeout: int = 10


settings = Settings()
