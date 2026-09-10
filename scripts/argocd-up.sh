#!/usr/bin/env bash
# Install Argo CD into the kind cluster and register the local Application.
#
# The repository is private, so Argo needs read credentials. They are taken
# from the GitHub CLI at run time and written straight into the cluster —
# never into Git.
set -euo pipefail

NAMESPACE="${NAMESPACE:-argocd}"
REPO_URL="${REPO_URL:-https://github.com/negativexq/ml-platform-infrastructure.git}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

step "Installing Argo CD"
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# Server-side apply: the ApplicationSet CRD exceeds the client-side annotation
# size limit and is silently rejected by a plain `kubectl apply`.
kubectl apply -n "$NAMESPACE" --server-side --force-conflicts \
  -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml >/dev/null
kubectl -n "$NAMESPACE" wait --for=condition=available deployment --all --timeout=600s

step "Registering repository credentials"
token="$(gh auth token)"
user="$(gh api user --jq .login)"
kubectl -n "$NAMESPACE" create secret generic repo-ml-platform \
  --from-literal=type=git \
  --from-literal=url="$REPO_URL" \
  --from-literal=username="$user" \
  --from-literal=password="$token" \
  --dry-run=client -o yaml \
  | kubectl label -f - --local -o yaml \
      argocd.argoproj.io/secret-type=repository \
  | kubectl apply -f - >/dev/null
echo "credentials registered for $user (token not written to Git)"

step "Applying the Application manifest"
kubectl apply -f gitops/applications/inference-local.yaml

step "Waiting for the Application to become Healthy"
for _ in $(seq 1 60); do
  sync=$(kubectl -n "$NAMESPACE" get application inference-local \
    -o jsonpath='{.status.sync.status}' 2>/dev/null || true)
  health=$(kubectl -n "$NAMESPACE" get application inference-local \
    -o jsonpath='{.status.health.status}' 2>/dev/null || true)
  echo "sync=${sync:-?} health=${health:-?}"
  [[ "$sync" == "Synced" && "$health" == "Healthy" ]] && break
  sleep 5
done

step "Admin password"
echo "user: admin"
echo -n "pass: "
kubectl -n "$NAMESPACE" get secret argocd-initial-admin-secret \
  -o jsonpath='{.data.password}' 2>/dev/null | base64 -d || echo "(already rotated)"
echo
echo
echo "UI: kubectl -n $NAMESPACE port-forward svc/argocd-server 8080:443"
