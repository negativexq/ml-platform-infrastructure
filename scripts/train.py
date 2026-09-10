"""M1 training: a deterministic run tracked in MLflow.

Parameters, metrics and the model artifact are logged to the MLflow tracking
server; metadata lands in PostgreSQL and the artifact in MinIO.

    python scripts/train.py --experiment ml-platform --register ml-platform-model
"""

from __future__ import annotations

import argparse
import os

import mlflow
import mlflow.sklearn
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split

SEED = 42
TRUE_COEF = np.array([1.5, -2.0, 0.5])
INTERCEPT = 0.1


def make_dataset(n: int = 500) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic synthetic regression dataset."""
    rng = np.random.default_rng(SEED)
    x = rng.normal(size=(n, TRUE_COEF.size))
    noise = rng.normal(scale=0.1, size=n)
    y = x @ TRUE_COEF + INTERCEPT + noise
    return x, y


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", default="ml-platform")
    parser.add_argument("--alpha", type=float, default=1.0)
    parser.add_argument("--samples", type=int, default=500)
    parser.add_argument(
        "--register",
        default=None,
        help="Registered model name; omit to log the artifact without registering.",
    )
    args = parser.parse_args()

    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5000")
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(args.experiment)

    x, y = make_dataset(args.samples)
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=0.2, random_state=SEED
    )

    with mlflow.start_run() as run:
        mlflow.log_params(
            {
                "model_type": "Ridge",
                "alpha": args.alpha,
                "samples": args.samples,
                "n_features": int(x.shape[1]),
                "seed": SEED,
            }
        )

        model = Ridge(alpha=args.alpha, random_state=SEED).fit(x_train, y_train)
        preds = model.predict(x_test)

        metrics = {
            "r2": float(r2_score(y_test, preds)),
            "mae": float(mean_absolute_error(y_test, preds)),
            "rmse": float(np.sqrt(mean_squared_error(y_test, preds))),
        }
        mlflow.log_metrics(metrics)
        mlflow.set_tag("stage", "candidate")

        mlflow.sklearn.log_model(
            sk_model=model,
            name="model",
            input_example=x_test[:2],
            registered_model_name=args.register,
        )

        print(f"run_id={run.info.run_id}")
        print(f"metrics={metrics}")
        print(f"model_uri=runs:/{run.info.run_id}/model")


if __name__ == "__main__":
    main()
