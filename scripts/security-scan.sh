#!/usr/bin/env bash
# M9 local security gates. Mirrors the CI job so a failure can be reproduced
# and fixed before pushing.
#
#   1. kubeconform   — rendered manifests are valid Kubernetes
#   2. trivy (gate)   — FAIL on a HIGH/CRITICAL that has a fix available
#   3. trivy (report) — full count including unfixed base-image CVEs (never fails)
#   4. trivy (secret) — no embedded credentials
#   5. syft SBOM      — written to sbom/ if syft is installed
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

IMAGES=(
  "ml-platform-inference:dev"
  "ml-platform-mlflow:local"
  "ml-platform-training:local"
)

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
rc=0

step "kubeconform — rendered manifests"
for chart in helm/ml-platform helm/platform-local; do
  values=()
  [[ -f "$chart/values-local.yaml" ]] && values=(--values "$chart/values-local.yaml")
  helm template x "$chart" ${values[@]+"${values[@]}"} \
    | kubeconform -strict -summary -skip ServiceMonitor \
        -schema-location default \
        -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
    || rc=1
done

step "trivy — gate on fixable HIGH/CRITICAL"
for img in "${IMAGES[@]}"; do
  echo "  $img"
  trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
    --exit-code 1 --quiet "$img" || rc=1
done

step "trivy — full report (unfixed base CVEs included; informational)"
for img in "${IMAGES[@]}"; do
  n=$(trivy image --scanners vuln --severity HIGH,CRITICAL --quiet -f json "$img" 2>/dev/null \
      | python3 -c 'import json,sys; d=json.load(sys.stdin); print(sum(len(r.get("Vulnerabilities") or []) for r in d.get("Results",[])))')
  echo "  $img : $n HIGH/CRITICAL total (see .trivyignore for the unfixed base set)"
done

step "trivy — embedded secrets"
for img in "${IMAGES[@]}"; do
  trivy image --scanners secret --exit-code 1 --quiet "$img" || rc=1
  echo "  $img : clean"
done

step "syft — SBOM"
if command -v syft >/dev/null; then
  mkdir -p sbom
  for img in "${IMAGES[@]}"; do
    out="sbom/${img//[:\/]/_}.spdx.json"
    syft "$img" -o spdx-json="$out" -q
    echo "  wrote $out"
  done
else
  echo "  syft not installed — skipping (CI generates the SBOM)"
fi

exit $rc
