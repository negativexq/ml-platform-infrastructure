#!/usr/bin/env bash
# Bring up the local kind cluster and deploy the inference workload onto it.
#
# The M1 platform services (MLflow, MinIO, PostgreSQL) stay in Docker Compose
# on the host; only the inference workload moves to Kubernetes.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-ml-platform}"
NAMESPACE="${NAMESPACE:-ml-platform}"
IMAGE="${IMAGE:-ml-platform-inference:dev}"
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

step "Detecting the address kind nodes use to reach the host"
# kind nodes sit on the 'kind' docker bridge; the host is its gateway. The
# network is dual-stack, so pick the IPv4 gateway explicitly — the IPv6 one
# comes first and would need bracket syntax everywhere downstream.
PLATFORM_HOST="$(docker network inspect kind -f '{{json .IPAM.Config}}' \
  | python3 -c 'import json,sys,ipaddress
for cfg in json.load(sys.stdin):
    gw = cfg.get("Gateway", "")
    if gw and isinstance(ipaddress.ip_address(gw), ipaddress.IPv4Address):
        print(gw); break' 2>/dev/null || true)"
if [[ -z "$PLATFORM_HOST" ]]; then
  echo "could not determine the kind network gateway" >&2
  exit 1
fi
echo "platform host: $PLATFORM_HOST"

step "Ensuring the M1 platform accepts requests from that address"
# MLflow 3.x rejects unknown Host headers, so the gateway must be allowlisted.
ALLOWED="mlflow:5000,mlflow,localhost:5001,localhost:5000,127.0.0.1:5001"
ALLOWED="${ALLOWED},${PLATFORM_HOST}:5001,${PLATFORM_HOST}"
if [[ -f .env ]]; then
  grep -v '^MLFLOW_ALLOWED_HOSTS=' .env > .env.tmp || true
  mv .env.tmp .env
fi
echo "MLFLOW_ALLOWED_HOSTS=${ALLOWED}" >> .env
docker compose up -d postgres minio minio-init mlflow >/dev/null
echo "compose platform services up"

step "Building and loading the image into the cluster"
docker build -q -t "$IMAGE" . >/dev/null
kind load docker-image "$IMAGE" --name "$CLUSTER_NAME"

step "Applying manifests"
kubectl apply -f k8s/base/namespace.yaml
kubectl apply -f k8s/base/serviceaccount.yaml
kubectl apply -f k8s/base/secret.yaml
for manifest in k8s/base/configmap.yaml k8s/base/deployment.yaml; do
  sed "s|__PLATFORM_HOST__|${PLATFORM_HOST}|g" "$manifest" | kubectl apply -f -
done
kubectl apply -f k8s/base/service.yaml

step "Waiting for rollout"
kubectl -n "$NAMESPACE" rollout status deployment/inference --timeout=300s

step "Cluster state"
kubectl -n "$NAMESPACE" get deploy,pod,svc -o wide

printf '\n\033[1mService:\033[0m http://localhost:30080\n'
