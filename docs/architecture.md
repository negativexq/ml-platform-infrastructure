# Architecture

## M0 — current state

```
client ──HTTP──> FastAPI (app.main)
                   ├── GET /health   liveness  (process alive)
                   ├── GET /ready    readiness (model loaded → 200, else 503)
                   └── POST /predict typed request → InferenceService.predict
                                        │
                                        └── model_loader.load_model()
                                              bounded retry → LoadedModel
                                              (M0: local artifacts/model.joblib)
```

- **`app/config.py`** — env-sourced `Settings` (`ML_` prefix).
- **`app/schemas.py`** — typed request/response models; invalid input →
  422 with a typed validation error.
- **`app/model_loader.py`** — `load_model()` with a bounded retry loop; failure
  is surfaced, not swallowed. M1 extends this to resolve an MLflow `MODEL_URI`
  and download from object storage.
- **`app/inference.py`** — holds the loaded model; `ready` is `False` until a
  successful load. Prediction failure never crashes the process.
- **`app/main.py`** — wires routes + structured logging via a lifespan hook.

## Target state (M6 contract)

| Local (M0–M5) | AWS (M6 design) |
| --- | --- |
| kind | EKS |
| Docker image | ECR |
| MinIO | S3 |
| PostgreSQL | RDS PostgreSQL |
| local Service | EKS networking / ALB |
| local identity | IAM workload identity |

See [roadmap.md](roadmap.md) for the milestone breakdown and gates.
