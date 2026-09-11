#!/usr/bin/env bash
# Wrap observability/alert-rules.yaml (the promtool-testable plain rule file)
# in a PrometheusRule CRD, so there is exactly one place the rules are
# written — no risk of the applied rules drifting from the tested ones.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

.venv/bin/python - <<'PY'
import yaml

with open("observability/alert-rules.yaml") as f:
    rules = yaml.safe_load(f)

crd = {
    "apiVersion": "monitoring.coreos.com/v1",
    "kind": "PrometheusRule",
    "metadata": {
        "name": "ml-platform-inference",
        "namespace": "ml-platform",
        "labels": {"release": "monitoring"},
    },
    "spec": rules,
}

with open("observability/prometheus-rule.generated.yaml", "w") as f:
    f.write("# Generated from observability/alert-rules.yaml by\n")
    f.write("# scripts/render-prometheus-rule.sh — do not edit by hand.\n")
    yaml.dump(crd, f, sort_keys=False, default_flow_style=False)

print("wrote observability/prometheus-rule.generated.yaml")
PY
