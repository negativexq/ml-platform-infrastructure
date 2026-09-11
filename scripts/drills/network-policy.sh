#!/usr/bin/env bash
# M9 drill — prove the NetworkPolicy matrix, allow AND deny, from pods that
# carry the same labels the real workloads do.
#
#   inference → mlflow:5000     ALLOW
#   inference → minio:9000      ALLOW
#   inference → postgres:5432   DENY     <- the application has no DB business
#   training  → mlflow:5000     ALLOW
#   training  → postgres:5432   DENY
#   mlflow    → postgres:5432   ALLOW
#   mlflow    → minio:9000      ALLOW
#   bystander → postgres:5432   DENY     <- unlabelled pod, nothing allows it
#   bystander → minio:9000      DENY
#
# MLflow's own port 5000 is intentionally reachable from any pod and the node
# port — it is the shared tracking API/UI. The precise controls are on the
# metadata database and the object store.
set -euo pipefail

NS="${NAMESPACE:-ml-platform}"
FAIL=0

# probe <name> <label-value> <host> <port> <expect: ALLOW|DENY>
probe() {
  local name="$1" label="$2" host="$3" port="$4" expect="$5"
  local overrides result
  if [[ -n "$label" ]]; then
    overrides='{"metadata":{"labels":{"app.kubernetes.io/name":"'"$label"'"}}}'
  else
    overrides='{}'
  fi
  # nc exit 0 = connected; timeout/refused = non-zero.
  if kubectl -n "$NS" run "$name" --rm -i --restart=Never --image=busybox:1.36 \
       --overrides="$overrides" --timeout=90s --quiet -- \
       sh -c "nc -z -w4 $host $port" >/dev/null 2>&1; then
    result=ALLOW
  else
    result=DENY
  fi
  if [[ "$result" == "$expect" ]]; then
    printf '  \033[32m OK \033[0m  %-26s %-22s -> %s\n' "$label→${host%%.*}" ":$port" "$result"
  else
    printf '  \033[31mFAIL\033[0m  %-26s %-22s -> got %s, want %s\n' "$label→${host%%.*}" ":$port" "$result" "$expect"
    FAIL=1
  fi
}

echo "NetworkPolicy matrix ($(kubectl -n "$NS" get networkpolicy --no-headers | wc -l | tr -d ' ') policies active)"
echo

probe np-inf-mlflow   ml-platform  platform-mlflow    5000  ALLOW
probe np-inf-minio    ml-platform  platform-minio     9000  ALLOW
probe np-inf-pg       ml-platform  platform-postgres  5432  DENY
probe np-train-mlflow train        platform-mlflow    5000  ALLOW
probe np-train-pg     train        platform-postgres  5432  DENY
probe np-mlflow-pg    mlflow       platform-postgres  5432  ALLOW
probe np-mlflow-minio mlflow       platform-minio     9000  ALLOW
probe np-bystand-pg   ""           platform-postgres  5432  DENY
probe np-bystand-s3   ""           platform-minio     9000  DENY

echo
if (( FAIL )); then
  echo "NETWORK POLICY DRILL FAILED"
  exit 1
fi
echo "all edges match the intended matrix"
