.PHONY: install train train-baseline test integration lint type check docker-build docker-run run clean \
        platform-up mlflow-ui kind-up kind-down smoke k8s-logs \
        helm-lint helm-template helm-deploy helm-rollback helm-history \
        argocd-up argocd-ui argocd-status backup restore drill-persistence drill-backup-restore \
        observability-up grafana prometheus verify-dashboards security-scan drill-netpol \
        alert-rules-test alert-rules-apply loadtest drill-autoscale drill-drain \
        local-up local-test local-down \
        tf-fmt tf-validate tf-lint tf-check \
        identity-up ui-install ui-build ui-api ui-dev cp-install cp-test cp-check cp-check-light cp-migrate cp-run cp-reconcile cp-gateway gateway-e2e cp-demo lock envtest-up envtest-down \
        cp-docker-build cp-helm-lint cp-helm-template cp-http-test cp-bootstrap-help cp-release-check cp-admission-check cp-limiter-check-help cp-recovery-check-help cp-cpu-acceptance-help cp-query-profile-help loadgen-build loadgen-test gateway-go-build gateway-go-test cache-clean kind-storage-status kind-storage-apply

CLUSTER_NAME ?= ml-platform

IMAGE ?= ml-platform-inference:dev

install:
	python -m pip install --upgrade pip
	pip install -e ".[dev,inference,train]"

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

## M9 security
security-scan:
	./scripts/security-scan.sh

drill-netpol:
	./scripts/drills/network-policy.sh

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

## M11 reproducibility
local-up:
	./scripts/local-up.sh

local-test:
	./scripts/local-test.sh

local-down:
	./scripts/kind-down.sh

## M10 scaling & alerting
alert-rules-test:
	cd observability && promtool test rules alert-rules.test.yaml controlplane-alert-rules.test.yaml

alert-rules-apply: alert-rules-test
	./scripts/render-prometheus-rule.sh
	kubectl apply -f observability/prometheus-rule.generated.yaml

loadtest:
	k6 run scripts/loadtest/predict.js

drill-autoscale:
	./scripts/drills/autoscaling.sh

drill-drain:
	./scripts/drills/node-drain.sh

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

## M13+ control plane (controlplane/). Its tests start an embedded PostgreSQL
## unless CP_TEST_DATABASE_URL points at one.
cp-install:
	pip install -c constraints/controlplane.txt -e ".[dev,controlplane,controlplane-dev]"

CP_IMAGE ?= mlp-controlplane:dev
CP_MIGRATION_IMAGE ?= mlp-controlplane-migrate:dev

.PHONY: cp-migration-docker-build
cp-migration-docker-build:
	docker build -t $(CP_MIGRATION_IMAGE) -f docker/controlplane-migrate/Dockerfile .

cp-docker-build:
	docker build -t $(CP_IMAGE) -f docker/controlplane/Dockerfile .

cp-helm-lint:
	helm lint helm/controlplane
	helm lint helm/controlplane --values helm/controlplane/values-local.yaml

cp-helm-template:
	helm template mlp helm/controlplane --namespace mlp-system

cp-test:
	pytest controlplane/tests -p no:warnings

cp-check:
	ruff check controlplane
	mypy controlplane
	pytest controlplane/tests -p no:warnings

cp-check-light:
	ruff check controlplane scripts/controlplane_*py
	mypy controlplane scripts/controlplane_*py
	pytest controlplane/tests --ignore=controlplane/tests/test_ui.py --ignore=controlplane/tests/test_ui_auth.py --ignore=controlplane/tests/test_persistence_pg.py -k 'not sql' -p no:warnings

cp-http-test:
	pytest controlplane/tests/test_gateway_http.py -p no:warnings

CP_RELEASE_OUT ?= /tmp/mlp-release-check
cp-release-check:
	python scripts/controlplane_release_check.py --build --image $(CP_IMAGE) --migration-image $(CP_MIGRATION_IMAGE) --out $(CP_RELEASE_OUT)

cp-admission-check:
	go version >/dev/null
	helm version --short >/dev/null
	pytest controlplane/tests/test_admission_policy.py -s

cp-cpu-acceptance-help:
	python scripts/controlplane_cpu_acceptance.py --help

cp-limiter-check-help:
	python scripts/controlplane_limiter_check.py --help

cp-query-profile-help:
	python scripts/controlplane_query_profile.py --help

cp-recovery-check-help:
	python scripts/controlplane_recovery_check.py --help

cp-bootstrap-help:
	python scripts/controlplane_bootstrap.py --help

cp-migrate:
	python -m controlplane.persistence.migrate upgrade

cp-run:
	CP_AUTH_MODE=$${CP_AUTH_MODE:-none} \
	CP_DATABASE_ROLE_ENFORCEMENT=$${CP_DATABASE_ROLE_ENFORCEMENT:-false} \
	CP_JOB_IMAGE_DIGEST_REQUIRED=$${CP_JOB_IMAGE_DIGEST_REQUIRED:-false} \
	uvicorn controlplane.main:app_factory --factory --reload --port 8080

cp-gateway:  # the inference gateway on :8081, against CP_DATABASE_URL (API keys only by default)
	CP_AUTH_MODE=$${CP_AUTH_MODE:-none} \
	CP_DATABASE_ROLE_ENFORCEMENT=$${CP_DATABASE_ROLE_ENFORCEMENT:-false} \
	CP_JOB_IMAGE_DIGEST_REQUIRED=$${CP_JOB_IMAGE_DIGEST_REQUIRED:-false} \
	uvicorn controlplane.gateway_main:app_factory --factory --reload --port 8081

gateway-e2e:  # real PostgreSQL + the real gateway process + a v2-protocol model server
	python scripts/gateway_e2e.py

cp-reconcile:
	python -m controlplane.reconciler_main

## Real kube-apiserver + etcd + controller-manager, no nodes (see scripts/envtest.sh)
envtest-up:
	./scripts/envtest.sh up

envtest-down:
	./scripts/envtest.sh down

## The UI on in-memory fakes: no PostgreSQL, Kubernetes, Argo, MLflow or KServe needed.
## http://localhost:8080  (Abort / Cancel work: a reconcile loop runs in the background)
## UI (controlplane/ui/web): the built bundle in controlplane/ui/static is committed
ui-install:
	cd controlplane/ui/web && npm ci

ui-build:
	cd controlplane/ui/web && npm run build

ui-api:  # regenerate the typed API client from the control plane's OpenAPI
	cd controlplane/ui/web && PYTHONPATH=$(CURDIR) npm run gen:api

ui-dev:  # hot reload on :5173, proxying the API to `make cp-demo` on :8080
	cd controlplane/ui/web && npm run dev

## Identity: Keycloak in kind with the local realm (alice, bob, carol), see docs/identity.md
identity-up:
	./scripts/identity-up.sh

cp-demo:
	python -m controlplane.demo

## Regenerate constraints/*.txt (pinned versions for the images and the control plane).
lock:
	./scripts/lock.sh

LOADGEN_OUT ?= /tmp/mlp-loadgen
loadgen-build:
	cd tools/loadgen && go build -trimpath -o $(LOADGEN_OUT) .

loadgen-test:
	cd tools/loadgen && go test -race ./... && go vet ./...

cache-clean:
	./scripts/cache-clean.sh

kind-storage-status:
	python3 scripts/kind-storage.py --cluster "$(CLUSTER_NAME)"

kind-storage-apply:
	python3 scripts/kind-storage.py --cluster "$(CLUSTER_NAME)" --apply

GATEWAY_GO_OUT ?= /tmp/mlp-gateway-go
gateway-go-build:
	cd services/gateway-go && go build -mod=readonly -trimpath -o $(GATEWAY_GO_OUT) .

gateway-go-test:
	cd services/gateway-go && go test -race ./... && go vet ./...

.PHONY: gateway-go-check cp-gateway-go-release-check
gateway-go-check:
	cd services/gateway-go && go test -race ./... && go vet ./...

cp-gateway-go-release-check:
	bash scripts/gateway-go-release-check.sh

.PHONY: storage-initializer-go-build storage-initializer-go-test
STORAGE_INITIALIZER_GO_OUT ?= /tmp/mlp-storage-initializer-go
storage-initializer-go-build:
	cd services/storage-initializer-go && go build -mod=readonly -trimpath -o $(STORAGE_INITIALIZER_GO_OUT) .

storage-initializer-go-test:
	cd services/storage-initializer-go && go test -race ./... && go vet ./...

.PHONY: log-stream-go-build log-stream-go-test
LOG_STREAM_GO_OUT ?= /tmp/mlp-log-stream-go
log-stream-go-build:
	cd services/log-stream-go && go build -mod=readonly -trimpath -o $(LOG_STREAM_GO_OUT) .

log-stream-go-test:
	cd services/log-stream-go && go test -race ./... && go vet ./...

.PHONY: cp-log-stream-check-help
cp-log-stream-check-help:
	$(PYTHON) scripts/controlplane_log_stream_check.py --help
