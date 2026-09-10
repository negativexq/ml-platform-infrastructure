#!/usr/bin/env bash
# Bring up the full local platform on kind — nothing runs in Docker Compose.
#
#   kind cluster
#     ├── platform-local : PostgreSQL + MinIO + MLflow   (helm/platform-local)
#     ├── training Job    : seeds ml-platform-model v1
#     └── inference       : helm/ml-platform, values-local.yaml
#
# Docker Compose is not touched by this script. `make argocd-up` migrates
# deployment authority to Git afterwards.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-ml-platform}"
NAMESPACE="${NAMESPACE:-ml-platform}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "Creating kind cluster '$CLUSTER_NAME' (idempotent)"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "cluster already exists"
else
  kind create cluster --config k8s/kind-cluster.yaml
fi
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null

step "Building images"
docker build -q -t ml-platform-inference:dev . >/dev/null
docker build -q -t ml-platform-mlflow:local docker/mlflow >/dev/null
docker build -q -t ml-platform-training:local -f docker/training/Dockerfile . >/dev/null

step "Loading images into the cluster"
kind load docker-image ml-platform-inference:dev ml-platform-mlflow:local \
  ml-platform-training:local --name "$CLUSTER_NAME"

step "Namespace"
kubectl apply -f k8s/base/namespace.yaml

step "Deploying platform-local (PostgreSQL + MinIO + MLflow)"
helm upgrade --install platform-local helm/platform-local \
  --namespace "$NAMESPACE" \
  --wait --timeout 5m
kubectl -n "$NAMESPACE" rollout status deployment/platform-mlflow --timeout=300s

step "Seeding the model registry (training Job)"
./scripts/train-job.sh 1.0

step "Deploying the inference service"
helm upgrade --install inference helm/ml-platform \
  --namespace "$NAMESPACE" \
  --values helm/ml-platform/values-local.yaml \
  --wait --timeout 5m

step "Cluster state"
kubectl -n "$NAMESPACE" get deploy,pod,svc -o wide

cat <<EOF

  inference : http://localhost:30080
  MLflow UI : http://localhost:30500
EOF
