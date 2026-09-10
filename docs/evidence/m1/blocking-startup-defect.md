# M1 failure engineering — blocking startup defect (found and fixed)

The M1 failure scenarios were meant to confirm the M0 contract still held with
a real artifact store behind the service. One of them broke it.

## Symptom

With the MLflow tracking server stopped, the container answered **nothing at
all** — not even `/health`:

```
$ docker compose stop mlflow
$ docker run -d --name m1nomlflow ... ml-platform-inference
$ curl -s -o /dev/null -w "HTTP %{http_code}\n" localhost:8083/health
HTTP 000
$ curl -s -o /dev/null -w "HTTP %{http_code}\n" localhost:8083/ready
HTTP 000
```

## Cause

`InferenceService.startup()` ran synchronously inside the FastAPI lifespan
hook, so uvicorn did not begin accepting connections until model loading
finished. MLflow's client adds its *own* retry budget (7 attempts with
backoff) inside each of our attempts, so the startup hook blocked for minutes:

```
Retrying (Retry(total=6, connect=6, ...)) after connection broken by
'NameResolutionError("HTTPConnection(host=\'mlflow\', port=5000): Failed to
resolve \'mlflow\'")': /api/2.0/mlflow/registered-models/get
```

## Why it matters

This is exactly the case M2 depends on. A liveness probe on `/health` would
get connection-refused, Kubernetes would kill the pod, and the Deployment
would CrashLoopBackOff — a dependency outage escalating into a restart storm —
instead of the intended behaviour: pod alive, readiness false, no traffic
routed.

## Fix

- Model loading moved to a background daemon thread
  ([`app/inference.py`](../../../app/inference.py)); the lifespan hook returns
  immediately. Explicit `loading` → `ready` / `failed` state, exposed on
  `/ready`.
- MLflow's own retry budget capped (`MLFLOW_HTTP_REQUEST_MAX_RETRIES=1`,
  `MLFLOW_HTTP_REQUEST_TIMEOUT=10`) so this repo's bounded loop is the only
  retry policy that matters.
- A background recheck loop (`ML_MODEL_LOAD_RECHECK_SECONDS`, default 30s)
  retries after the initial burst fails, so the service recovers without a
  restart.
- Regression test:
  `tests/test_api.py::test_health_stays_200_while_model_unavailable`.

## Verified after the fix

MLflow down, 4 seconds after container start:

```
/health HTTP 200
/ready  HTTP 503  {"ready":false,"state":"failed", "detail":"API request to
                   http://mlflow:5000/api ... failed"}
```

Self-recovery, MLflow restarted, recheck interval 10s, **no container restart**:

```
t+5s  /ready HTTP 503
t+10s /ready HTTP 503
t+15s /ready HTTP 200
{"ready":true,"state":"ready","model_version":"models:/ml-platform-model/1", ...}
/predict → {"prediction":-0.9069787623077947, ...} HTTP 200
```

## Other M1 failure scenarios

| Scenario | Observed |
| --- | --- |
| Wrong artifact URI (`models:/ml-platform-model/999`) | `/health` 200 · `/ready` 503 `RESOURCE_DOES_NOT_EXIST: Model Version (name=ml-platform-model, version=999) not found` · `/predict` 503 `model not ready` |
| Artifact store (MinIO) stopped | `/health` 200 · `/ready` `ready:false`, detail `Could not connect to the endpoint URL "http://minio:9000/mlflow-artifacts?list-type=2&..."` |
| Tracking server (MLflow) stopped | `/health` 200 · `/ready` 503 (see above) |
| Correct artifact restored | recovers to `/ready` 200 within one recheck interval, no restart |
