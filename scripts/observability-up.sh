#!/usr/bin/env bash
# Install Prometheus + Grafana into the kind cluster and load the dashboards.
set -euo pipefail

NAMESPACE="${NAMESPACE:-observability}"
RELEASE="${RELEASE:-monitoring}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "Installing kube-prometheus-stack"
helm repo add prometheus-community \
  https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
helm upgrade --install "$RELEASE" prometheus-community/kube-prometheus-stack \
  --namespace "$NAMESPACE" --create-namespace \
  --values observability/kube-prometheus-stack-values.yaml \
  --wait --timeout 15m

step "Loading dashboards"
# The Grafana sidecar imports any ConfigMap labelled grafana_dashboard.
for dashboard in observability/dashboards/*.json; do
  name="grafana-dashboard-$(basename "$dashboard" .json)"
  kubectl -n "$NAMESPACE" create configmap "$name" \
    --from-file="$(basename "$dashboard")=$dashboard" \
    --dry-run=client -o yaml \
    | kubectl label -f - --local -o yaml grafana_dashboard=1 \
    | kubectl apply -f - >/dev/null
  echo "loaded $name"
done

step "Ready"
cat <<EOF
Grafana:    kubectl -n $NAMESPACE port-forward svc/${RELEASE}-grafana 3000:80
            http://localhost:3000  (admin / admin)
Prometheus: kubectl -n $NAMESPACE port-forward svc/${RELEASE}-prometheus 9090:9090
            http://localhost:9090
EOF
