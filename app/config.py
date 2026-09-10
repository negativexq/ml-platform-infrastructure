"""Application configuration, sourced from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ML_", env_file=".env", extra="ignore")

    app_name: str = "ml-platform-inference"
    log_level: str = "INFO"
    log_json: bool = True

    # M0: local model file. M1 replaces this with an MLflow/MinIO artifact URI.
    model_path: str = "artifacts/model.joblib"
    model_uri: str | None = None

    # Bounded startup behaviour for model loading (used from M1 onwards).
    model_load_max_retries: int = 5
    model_load_retry_seconds: float = 2.0


settings = Settings()
