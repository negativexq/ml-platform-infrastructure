#!/usr/bin/env bash
# Acceptance suite for a bootstrapped local platform (`make local-up` first).
#
# Runs the fast, repeatable checks every time. The slow/disruptive drills
# (full backup-restore, the k6 autoscaling ramp, a real node drain) are
# exercised in CI/evidence separately and are not run here by default — this
# suite is meant to be re-run often, including against a cluster with real
# traffic already on it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
NS="${NAMESPACE:-ml-platform}"
FAIL=0

section() { printf '\n\033[1m-- %s --\033[0m\n' "$1"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=1; }
run()  { local name="$1"; shift; if "$@" >/tmp/local-test-"$RANDOM".log 2>&1; then ok "$name"; else bad "$name"; fi; }

section "GitOps"
sync=$(kubectl -n argocd get application inference-local -o jsonpath='{.status.sync.status}' 2>/dev/null || echo '?')
health=$(kubectl -n argocd get application inference-local -o jsonpath='{.status.health.status}' 2>/dev/null || echo '?')
[[ "$sync" == "Synced" && "$health" == "Healthy" ]] && ok "inference-local Application Synced/Healthy" \
  || bad "inference-local Application: sync=$sync health=$health"

psync=$(kubectl -n argocd get application platform-local -o jsonpath='{.status.sync.status}' 2>/dev/null || echo '?')
phealth=$(kubectl -n argocd get application platform-local -o jsonpath='{.status.health.status}' 2>/dev/null || echo '?')
[[ "$psync" == "Synced" && "$phealth" == "Healthy" ]] && ok "platform-local Application Synced/Healthy" \
  || bad "platform-local Application: sync=$psync health=$phealth"

section "Service contract"
run "smoke test (/health, /ready, /predict, replicas)" ./scripts/smoke-test.sh

section "ML lifecycle"
run "integration tests (MLflow, registry, prediction)" env RUN_INTEGRATION=1 \
  MLFLOW_URL=http://localhost:30500 INFERENCE_URL=http://localhost:30080 \
  .venv/bin/pytest tests/integration -p no:warnings -q

section "Persistence (M8)"
run "pod-delete drill: data survives" env NAMESPACE="$NS" ./scripts/drills/persistence.sh

section "Security (M9)"
run "NetworkPolicy allow/deny matrix" env NAMESPACE="$NS" ./scripts/drills/network-policy.sh
psa=$(kubectl get ns "$NS" -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}' 2>/dev/null)
[[ "$psa" == "restricted" ]] && ok "namespace enforces Pod Security Standards: restricted" \
  || bad "namespace PSS label missing or not 'restricted' (got: ${psa:-none})"

section "Scaling & alerting (M10)"
hpa=$(kubectl -n "$NS" get hpa ml-platform-inference -o jsonpath='{.spec.minReplicas}/{.spec.maxReplicas}' 2>/dev/null)
[[ -n "$hpa" ]] && ok "HPA present (min/max: $hpa)" || bad "HPA not found"
pdb=$(kubectl -n "$NS" get pdb ml-platform-inference -o jsonpath='{.spec.minAvailable}' 2>/dev/null)
[[ -n "$pdb" ]] && ok "PodDisruptionBudget present (minAvailable: $pdb)" || bad "PDB not found"
run "alert rules unit tests" bash -c "cd observability && promtool test rules alert-rules.test.yaml"
rule_health=$(kubectl -n ml-platform get prometheusrule ml-platform-inference >/dev/null 2>&1 && echo present || echo missing)
[[ "$rule_health" == "present" ]] && ok "PrometheusRule applied" || bad "PrometheusRule missing"

section "Observability (M5)"
kubectl -n observability port-forward svc/monitoring-prometheus 9090:9090 >/tmp/local-test-promfwd.log 2>&1 &
PROM_PF=$!
trap 'kill $PROM_PF 2>/dev/null' EXIT
sleep 4
run "dashboard queries return live data" ./scripts/verify-dashboard-queries.sh
kill $PROM_PF 2>/dev/null || true

echo
if (( FAIL )); then
  echo "LOCAL ACCEPTANCE: FAILED"
  exit 1
fi
echo "LOCAL ACCEPTANCE: PASSED"
