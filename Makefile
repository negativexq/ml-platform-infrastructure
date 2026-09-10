.PHONY: install train train-baseline test integration lint type check docker-build docker-run run clean \
        platform-up mlflow-ui kind-up kind-down smoke k8s-logs \
        helm-lint helm-template helm-deploy helm-rollback helm-history \
        argocd-up argocd-ui argocd-status backup restore drill-persistence drill-backup-restore \
        observability-up grafana prometheus verify-dashboards \
        tf-fmt tf-validate tf-lint tf-check

IMAGE ?= ml-platform-inference:dev

install:
	python -m pip install --upgrade pip
	pip install -e ".[dev,train]"

## M7: the whole platform (PostgreSQL + MinIO + MLflow) runs inside kind.
## helm/platform-local deploys it; `make kind-up` does this end to end.
platform-up:
	helm upgrade --install platform-local helm/platform-local \
	  --namespace ml-platform --create-namespace --wait --timeout 5m

mlflow-ui:
	@echo "MLflow UI:      http://localhost:30500"
	@echo "MinIO console:  kubectl -n ml-platform port-forward svc/platform-minio 9001:9001"

## Train a tracked run inside the cluster and register a new model version
train:
	./scripts/train-job.sh

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

## M3 Helm
helm-lint:
	helm lint helm/ml-platform --values helm/ml-platform/values-local.yaml

helm-template:
	helm template inference helm/ml-platform --values helm/ml-platform/values-local.yaml

helm-deploy:
	./scripts/helm-deploy.sh

helm-history:
	helm -n ml-platform history inference

helm-rollback:
	helm -n ml-platform rollback inference --wait --timeout 5m

## M8 persistence & recovery
backup:
	./scripts/backup.sh

restore:
	./scripts/restore.sh $(SRC)

drill-persistence:
	./scripts/drills/persistence.sh

drill-backup-restore:
	./scripts/drills/backup-restore.sh

## M4 GitOps
argocd-up:
	./scripts/argocd-up.sh

argocd-status:
	kubectl -n argocd get applications \
	  -o custom-columns=NAME:.metadata.name,SYNC:.status.sync.status,HEALTH:.status.health.status,REVISION:.status.sync.revision

argocd-ui:
	@echo "https://localhost:8080  (user: admin)"
	@echo "password: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
	kubectl -n argocd port-forward svc/argocd-server 8080:443

## M5 observability
observability-up:
	./scripts/observability-up.sh

grafana:
	@echo "http://localhost:3000  (admin / admin)"
	kubectl -n observability port-forward svc/monitoring-grafana 3000:80

prometheus:
	@echo "http://localhost:9090"
	kubectl -n observability port-forward svc/monitoring-prometheus 9090:9090

verify-dashboards:
	./scripts/verify-dashboard-queries.sh

## M6 Terraform (design only — no apply)
TF_ENV ?= infra/terraform/environments/aws-dev

tf-fmt:
	terraform fmt -check -recursive infra/terraform

tf-validate:
	terraform -chdir=$(TF_ENV) init -backend=false -input=false
	terraform -chdir=$(TF_ENV) validate

tf-lint:
	tflint --init
	tflint --recursive --chdir=infra/terraform

tf-check: tf-fmt tf-validate tf-lint

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache artifacts
