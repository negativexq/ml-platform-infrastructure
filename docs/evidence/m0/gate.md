# M0 Gate — evidence

Run date: 2026-09-10 · Python 3.12 (venv) · Docker Desktop (linux/arm64)

## Quality gates

```
$ ruff check .
All checks passed!

$ mypy app
Success: no issues found in 6 source files

$ pytest
6 passed in 0.07s

$ docker build -t ml-platform-inference:dev .
exporting to image ... DONE
```

## Container startup + endpoint contract

Container run from the built image, no local artifact mounted (the baseline
model is baked at build time).

```
$ curl -s -w '\nHTTP %{http_code}\n' localhost:8000/health
{"status":"ok"}
HTTP 200

$ curl -s -w '\nHTTP %{http_code}\n' localhost:8000/ready
{"ready":true,"model_version":"model","detail":null}
HTTP 200

$ curl -s -X POST localhost:8000/predict -H 'content-type: application/json' \
    -d '{"features":[1.0,2.0,3.0]}'
{"prediction":-0.9000000000000027,"model_version":"model"}
HTTP 200

$ curl -s -X POST localhost:8000/predict -H 'content-type: application/json' \
    -d '{"features":[]}'
{"detail":[{"type":"too_short","loc":["body","features"],
  "msg":"List should have at least 1 item after validation, not 0", ...}]}
HTTP 422
```

## Readiness under a missing model

Covered by `tests/test_api.py::test_ready_503_when_model_missing` and
`::test_predict_503_when_not_ready` — with `ML_MODEL_PATH` pointing at a
nonexistent file, the process stays alive (`/health 200`) while `/ready`
returns `503` and `/predict` refuses with `503`. This is the behaviour M2
relies on to keep a bad pod out of the Service.

## Gate result

| Check | Result |
| --- | --- |
| pytest | PASS (6) |
| ruff | PASS |
| mypy | PASS |
| docker build | PASS |
| container startup | PASS |
| `/health` | PASS (200) |
| `/ready` | PASS (200 loaded / 503 missing) |
| `/predict` | PASS (200 valid / 422 invalid / 503 not ready) |
