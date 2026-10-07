"""Fail the image build/release gate if inference packages leak into its runtime."""

import importlib.metadata
import json

# Include the heavy transitive chains, not just the direct inference extra.
FORBIDDEN = {
    "numpy",
    "scipy",
    "scikit-learn",
    "pandas",
    "skops",
    "joblib",
    "threadpoolctl",
    "boto3",
    "botocore",
    "s3transfer",
    "jmespath",
    "prometheus-client",
}
REQUIRED = {
    "fastapi",
    "uvicorn",
    "pydantic",
    "pydantic-settings",
    "structlog",
    "mlflow-skinny",
    "kubernetes",
    "sqlalchemy",
    "alembic",
    "psycopg",
    "psycopg-binary",
    "pyjwt",
    "cryptography",
    "httpx",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-http",
    "opentelemetry-instrumentation-sqlalchemy",
    "opentelemetry-instrumentation-httpx",
}


def main() -> None:
    packages = {
        dist.metadata["Name"].lower().replace("_", "-"): dist.version
        for dist in importlib.metadata.distributions()
    }
    forbidden = sorted(FORBIDDEN & packages.keys())
    missing = sorted(REQUIRED - packages.keys())
    print(
        json.dumps(
            {
                "package_count": len(packages),
                "packages": dict(sorted(packages.items())),
                "forbidden_present": forbidden,
                "required_missing": missing,
            },
            sort_keys=True,
        )
    )
    if forbidden or missing:
        raise SystemExit("control-plane runtime dependency isolation failed")


if __name__ == "__main__":
    main()
