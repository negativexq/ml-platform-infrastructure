# M1 Gate — evidence

Run date: 2026-09-10 · stack: `docker compose` (PostgreSQL 16, MinIO, MLflow 3.16.0, inference)

Host port notes: MLflow is published on **5001** (macOS AirPlay Receiver holds
5000) and PostgreSQL is **not** published at all (another container on this
machine already owns 5432; MLflow reaches it over the compose network).

## Training run

```
$ make train
Successfully registered model 'ml-platform-model'.
Created version '1' of model 'ml-platform-model'.
run_id=d2faa4c4922349f68956520c7a6c286e
metrics={'r2': 0.9984040871752516, 'mae': 0.07600518323513163, 'rmse': 0.09519293509032241}
model_uri=runs:/d2faa4c4922349f68956520c7a6c286e/model
```

## Metadata persisted in PostgreSQL

```
$ docker compose exec postgres psql -U mlflow -d mlflow \
    -c "select key, value from metrics order by key;" \
    -c "select name, version from model_versions;"

 key  |        value
------+---------------------
 mae  | 0.07600518323513163
 r2   |  0.9984040871752516
 rmse | 0.09519293509032241

       name        | version
-------------------+---------
 ml-platform-model |       1
```

## Artifact persisted in MinIO

```
$ docker compose exec minio mc ls --recursive local/mlflow-artifacts/
[...] 1.1KiB 1/models/m-320092d0d05141c889803ad7c25d2292/artifacts/MLmodel
[...] 6.1KiB 1/models/m-320092d0d05141c889803ad7c25d2292/artifacts/model.skops
[...]   225B 1/models/m-320092d0d05141c889803ad7c25d2292/artifacts/conda.yaml
[...]   103B 1/models/m-320092d0d05141c889803ad7c25d2292/artifacts/requirements.txt
```

No model is baked into the inference image — the Dockerfile no longer runs a
training step. The artifact is pulled at startup.

## Inference downloads the artifact and serves it

```
$ curl -s localhost:8000/ready
{"ready":true,"state":"ready","model_version":"models:/ml-platform-model/1",
 "model_source":"mlflow","detail":null}                              HTTP 200

$ curl -s -X POST localhost:8000/predict -H 'content-type: application/json' \
    -d '{"features":[1.0,2.0,3.0]}'
{"prediction":-0.9069787623077947,"model_version":"models:/ml-platform-model/1"}
```

## Model version change picks up the new artifact

```
$ make train ALPHA=5.0        # → Created version '2'
$ ML_MODEL_URI=models:/ml-platform-model/2 docker compose up -d --force-recreate inference

{"ready":true,"state":"ready","model_version":"models:/ml-platform-model/2", ...}
{"prediction":-0.8903758188500281,"model_version":"models:/ml-platform-model/2"}
```

v1 predicted `-0.9069787623077947`, v2 predicts `-0.8903758188500281` for the
same input — a genuinely different artifact, not a cached one.

## Integration tests

```
$ make integration
4 passed
```

## Gate result

| Check | Result |
| --- | --- |
| training | PASS |
| MLflow experiment visible | PASS (`http://localhost:5001`, experiment `ml-platform`) |
| metadata persisted in PostgreSQL | PASS |
| artifact persisted in MinIO | PASS |
| inference downloads artifact | PASS |
| `/ready` becomes healthy | PASS |
| `/predict` succeeds | PASS |
| model version change loads new artifact | PASS |
