# ML Platform Infrastructure

A hands-on lab that hardens a containerized inference service locally, then
designs its AWS migration as IaC. Built milestone by milestone — each closes
with a gate and real evidence under [`docs/evidence/`](docs/evidence/).

## Milestone status

| Milestone | | Status |
| --- | --- | --- |
| M0 | Application & repository foundation | ✅ |
| M1 | Local ML lifecycle (MLflow + PostgreSQL + MinIO) | ✅ |
| M2 | Local Kubernetes with kind | ✅ |
| M3 | Helm packaging | ✅ |
| M4 | GitOps with Argo CD | ✅ |
| M5 | Observability & failure engineering | ⬜ |
| M6 | Terraform & AWS migration design | ⬜ |

Full plan, gates and contracts: [`docs/roadmap.md`](docs/roadmap.md) ·
architecture: [`docs/architecture.md`](docs/architecture.md).

## Quickstart

```bash
make install        # install app + dev deps
make train          # write artifacts/model.joblib (M0 baseline)
make run            # uvicorn on :8000
make check          # ruff + mypy + pytest
make docker-build   # build the image
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Liveness — process is up |
| GET | `/ready` | Readiness — `200` when the model is loaded, `503` otherwise |
| POST | `/predict` | `{"features": [f0, f1, ...]}` → `{"prediction": float, "model_version": str}` |

```bash
curl localhost:8000/health
curl localhost:8000/ready
curl -X POST localhost:8000/predict \
  -H 'content-type: application/json' -d '{"features":[1.0,2.0,3.0]}'
```

## M0 gate

`pytest` · `ruff` · `mypy` · `docker build` · container startup · `/health` ·
`/ready` · `/predict` — all pass. Verified in CI
([`.github/workflows/ci.yml`](.github/workflows/ci.yml)).
