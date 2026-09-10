"""M0 placeholder training: fit a tiny deterministic model and persist it.

This is intentionally trivial. M1 replaces it with an MLflow-tracked run whose
artifact is stored in MinIO/S3.
"""

from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import LinearRegression

OUT = Path("artifacts/model.joblib")


def main() -> None:
    rng = np.random.default_rng(42)
    x = rng.normal(size=(200, 3))
    y = x @ np.array([1.5, -2.0, 0.5]) + 0.1
    model = LinearRegression().fit(x, y)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, OUT)
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
