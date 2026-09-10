#!/usr/bin/env bash
# Run a training job inside the cluster and stream its logs.
#
#   ./scripts/train-job.sh [alpha]
set -euo pipefail

ALPHA="${1:-1.0}"
NAMESPACE="${NAMESPACE:-ml-platform}"
SUFFIX="$(date +%s)"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

sed -e "s|__ALPHA__|${ALPHA}|g" -e "s|__JOB_SUFFIX__|${SUFFIX}|g" \
  k8s/jobs/train.yaml | kubectl apply -f -

job="train-${SUFFIX}"
echo "waiting for ${job} to start..."
kubectl -n "$NAMESPACE" wait --for=condition=ready pod \
  -l "job-name=${job}" --timeout=120s 2>/dev/null || true

kubectl -n "$NAMESPACE" logs -f "job/${job}" || true

if kubectl -n "$NAMESPACE" wait --for=condition=complete "job/${job}" --timeout=300s 2>/dev/null; then
  echo "training job ${job} completed"
else
  echo "training job ${job} did not complete" >&2
  kubectl -n "$NAMESPACE" describe "job/${job}" | tail -20
  exit 1
fi
