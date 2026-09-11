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
#
# The probe pods must themselves satisfy the namespace's Pod Security Standard
# (M9: `restricted`) — otherwise the API server rejects the pod outright, the
# probe never runs, and that admission failure is indistinguishable from a
# real network DENY unless it is checked for explicitly. This bit the first
# run of this script after PSS was enforced: every "should ALLOW" edge came
# back DENY, because no probe pod was ever admitted. See docs/evidence/m11.
set -euo pipefail

NS="${NAMESPACE:-ml-platform}"
FAIL=0

PSS_SECURITY_CONTEXT='"securityContext":{"runAsNonRoot":true,"runAsUser":1000,"seccompProfile":{"type":"RuntimeDefault"},"allowPrivilegeEscalation":false,"capabilities":{"drop":["ALL"]}}'

# probe <name> <label-value> <host> <port> <expect: ALLOW|DENY>
probe() {
  local name="$1" label="$2" host="$3" port="$4" expect="$5"
  local labels_json overrides out result rc

  if [[ -n "$label" ]]; then
    labels_json='{"app.kubernetes.io/name":"'"$label"'"}'
  else
    labels_json='{}'
  fi

  overrides='{"metadata":{"labels":'"$labels_json"'},"spec":{'"$PSS_SECURITY_CONTEXT"',"containers":[{"name":"'"$name"'","image":"busybox:1.36","command":["sh","-c","nc -z -w4 '"$host"' '"$port"'"],'"$PSS_SECURITY_CONTEXT"'}]}}'

  out=$(kubectl -n "$NS" run "$name" --rm -i --restart=Never --image=busybox:1.36 \
          --overrides="$overrides" --timeout=90s --quiet 2>&1) && rc=0 || rc=$?

  if grep -qE "Forbidden|violates PodSecurity" <<<"$out"; then
    printf '  \033[31mERROR\033[0m %-26s pod rejected by admission control (not a network result):\n          %s\n' \
      "$label→${host%%.*}" "$(head -1 <<<"$out")"
    FAIL=1
    return
  fi

  [[ $rc -eq 0 ]] && result=ALLOW || result=DENY

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
