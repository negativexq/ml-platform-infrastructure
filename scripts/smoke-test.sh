#!/usr/bin/env bash
# Smoke-test the deployed inference workload through the Kubernetes Service.
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:30080}"
NAMESPACE="${NAMESPACE:-ml-platform}"
failures=0

check() {
  local name="$1" expected="$2" actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    printf '  \033[32mPASS\033[0m  %-34s %s\n' "$name" "$actual"
  else
    printf '  \033[31mFAIL\033[0m  %-34s got %s, want %s\n' "$name" "$actual" "$expected"
    failures=$((failures + 1))
  fi
}

code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "Smoke test against ${BASE_URL}"

check "GET /health" 200 "$(code "${BASE_URL}/health")"
check "GET /ready" 200 "$(code "${BASE_URL}/ready")"
check "POST /predict (valid)" 200 "$(code -X POST "${BASE_URL}/predict" \
  -H 'content-type: application/json' -d '{"features":[1.0,2.0,3.0]}')"
check "POST /predict (invalid)" 422 "$(code -X POST "${BASE_URL}/predict" \
  -H 'content-type: application/json' -d '{"features":[]}')"

ready_replicas="$(kubectl -n "$NAMESPACE" get deploy inference \
  -o jsonpath='{.status.readyReplicas}')"
desired="$(kubectl -n "$NAMESPACE" get deploy inference \
  -o jsonpath='{.spec.replicas}')"
check "ready replicas" "$desired" "$ready_replicas"

endpoints="$(kubectl -n "$NAMESPACE" get endpointslice \
  -l kubernetes.io/service-name=inference \
  -o jsonpath='{range .items[*].endpoints[*]}{.conditions.ready}{"\n"}{end}' \
  | grep -c true || true)"
check "ready Service endpoints" "$desired" "$endpoints"

echo
if (( failures > 0 )); then
  echo "smoke test FAILED ($failures)"
  exit 1
fi
echo "smoke test PASSED"
