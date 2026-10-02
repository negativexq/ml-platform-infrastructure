#!/usr/bin/env bash
# Wrap each plain, promtool-testable rule file in observability/ in a PrometheusRule CRD,
# so every rule is written in exactly one place: no risk of the applied rules drifting from
# the tested ones.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PYTHON="${PYTHON:-.venv/bin/python}"
[ -x "$PYTHON" ] || PYTHON=python3

"$PYTHON" - <<'PY'
import yaml

# source rule file -> (PrometheusRule name, namespace, generated file)
RULESETS = [
    ("observability/alert-rules.yaml", "ml-platform-inference", "ml-platform",
     "observability/prometheus-rule.generated.yaml"),
    ("observability/controlplane-alert-rules.yaml", "ml-platform-controlplane", "observability",
     "observability/controlplane-prometheus-rule.generated.yaml"),
]

for source, name, namespace, target in RULESETS:
    with open(source) as f:
        rules = yaml.safe_load(f)
    crd = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "PrometheusRule",
        "metadata": {"name": name, "namespace": namespace, "labels": {"release": "monitoring"}},
        "spec": rules,
    }
    with open(target, "w") as f:
        f.write(f"# Generated from {source} by\n")
        f.write("# scripts/render-prometheus-rule.sh — do not edit by hand.\n")
        yaml.dump(crd, f, sort_keys=False, default_flow_style=False)
    print(f"wrote {target}")
PY
