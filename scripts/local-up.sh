#!/usr/bin/env bash
# One-command bootstrap of the entire local platform, from a repo checkout
# and nothing else. No manually created cluster resource, no state assumed
# to already exist.
#
#   kind cluster
#     └── Argo CD (deployment authority, from Git)
#           ├── platform-local  : PostgreSQL + MinIO + MLflow
#           └── inference       : the application
#     └── training Job (seeds the model registry — not GitOps state)
#     └── kube-prometheus-stack + dashboards + PrometheusRule
#
# Run `make local-test` afterward for the acceptance suite.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
CLUSTER_NAME="${CLUSTER_NAME:-ml-platform}"
NS="${NAMESPACE:-ml-platform}"

step() { printf '\n\033[1m========== %s ==========\033[0m\n' "$1"; }
START=$(date +%s)

step "1/6  kind cluster"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  echo "cluster already exists"
else
  kind create cluster --config k8s/kind-cluster.yaml
fi
kubectl config use-context "kind-${CLUSTER_NAME}" >/dev/null

step "2/6  build and load images"
docker build -q -t ml-platform-inference:dev . >/dev/null
docker build -q -t ml-platform-mlflow:local docker/mlflow >/dev/null
docker build -q -t ml-platform-training:local -f docker/training/Dockerfile . >/dev/null
kind load docker-image ml-platform-inference:dev ml-platform-mlflow:local \
  ml-platform-training:local --name "$CLUSTER_NAME"

step "3/6  namespace (Pod Security Standards: restricted)"
kubectl apply -f k8s/base/namespace.yaml

step "4/6  Argo CD — deployment authority from Git"
./scripts/argocd-up.sh
kubectl -n "$NS" wait --for=condition=ready pod -l ml-platform.io/tier=platform --timeout=300s

step "5/6  seed the model registry, then let inference come up"
./scripts/train-job.sh 1.0
kubectl -n "$NS" rollout restart deployment/ml-platform-inference
kubectl -n "$NS" rollout status deployment/ml-platform-inference --timeout=300s
for _ in $(seq 1 30); do
  sync=$(kubectl -n argocd get application inference-local -o jsonpath='{.status.sync.status}' 2>/dev/null)
  health=$(kubectl -n argocd get application inference-local -o jsonpath='{.status.health.status}' 2>/dev/null)
  echo "inference-local: sync=${sync:-?} health=${health:-?}"
  [[ "$sync" == "Synced" && "$health" == "Healthy" ]] && break
  sleep 5
done

step "6/6  observability"
./scripts/observability-up.sh
./scripts/render-prometheus-rule.sh
kubectl apply -f observability/prometheus-rule.generated.yaml
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml >/dev/null
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' \
  >/dev/null 2>&1 || true

echo
echo "=========================================="
echo " bootstrap complete in $(( $(date +%s) - START ))s"
echo "   inference : http://localhost:30080"
echo "   MLflow UI : http://localhost:30500"
echo "   Grafana   : kubectl -n observability port-forward svc/monitoring-grafana 3000:80"
echo "   next      : make local-test"
echo "=========================================="
