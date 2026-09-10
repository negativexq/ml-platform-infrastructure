.PHONY: install train train-baseline test integration lint type check docker-build docker-run run clean \
        platform-up platform-down platform-logs mlflow-ui kind-up kind-down smoke k8s-logs

IMAGE ?= ml-platform-inference:dev
MLFLOW_TRACKING_URI ?= http://localhost:5001
MLFLOW_S3_ENDPOINT_URL ?= http://localhost:9000
AWS_ACCESS_KEY_ID ?= minioadmin
AWS_SECRET_ACCESS_KEY ?= minioadmin
export MLFLOW_TRACKING_URI MLFLOW_S3_ENDPOINT_URL AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY

install:
	python -m pip install --upgrade pip
	pip install -e ".[dev,train]"

## M1 local platform: PostgreSQL + MinIO + MLflow + inference
platform-up:
	docker compose up -d --build

platform-down:
	docker compose down

platform-logs:
	docker compose logs -f

mlflow-ui:
	@echo "MLflow UI: $(MLFLOW_TRACKING_URI)"
	@echo "MinIO console: http://localhost:9001 (minioadmin/minioadmin)"

## Train a tracked run and register a new model version
train:
	python scripts/train.py --register ml-platform-model

## M0 fallback: a local joblib artifact, no MLflow required
train-baseline:
	python scripts/train_baseline.py

test:
	pytest

integration:
	RUN_INTEGRATION=1 pytest tests/integration -p no:warnings

lint:
	ruff check .

type:
	mypy app

check: lint type test

run: train-baseline
	uvicorn app.main:app --reload --port 8000

docker-build:
	docker build -t $(IMAGE) .

docker-run:
	docker run --rm -p 8000:8000 $(IMAGE)

## M2 local Kubernetes
kind-up:
	./scripts/kind-up.sh

kind-down:
	./scripts/kind-down.sh

smoke:
	./scripts/smoke-test.sh

k8s-logs:
	kubectl -n ml-platform logs -l app.kubernetes.io/name=inference --tail=50 -f

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache artifacts
