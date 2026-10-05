#!/usr/bin/env python3
"""Verify installed policies and direct-SA denials using server dry runs only."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from copy import deepcopy
from typing import Any
from uuid import uuid4

POLICIES = ("namespace-owner", "project-writes", "project-rbac", "workflow-role")


def verify(context: str, release: str, system_namespace: str, project_namespace: str) -> int:
    name = (release + "-controlplane")[:50].rstrip("-")
    prefix = f"{system_namespace}-{name}"
    actor = f"system:serviceaccount:{system_namespace}:{name}-reconciler"

    def run(*args: str, body: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["kubectl", "--context", context, *args],
            input=json.dumps(body) if body else None,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

    def read(resource: str, *args: str) -> dict[str, Any]:
        result = run("get", resource, *args, "-o", "json")
        if result.returncode:
            raise RuntimeError("Cannot read admission gate resource: " + resource)
        value: dict[str, Any] = json.loads(result.stdout)
        return value

    for suffix in POLICIES:
        policy = read("validatingadmissionpolicy/" + prefix + "-" + suffix)
        status = policy.get("status", {})
        if (
            policy["spec"].get("failurePolicy") != "Fail"
            or status.get("observedGeneration") != policy["metadata"].get("generation")
            or "typeChecking" not in status
            or status["typeChecking"].get("expressionWarnings")
        ):
            raise RuntimeError("Policy is not type-checked and fail-closed: " + suffix)
        binding = read("validatingadmissionpolicybinding/" + prefix + "-" + suffix)
        if binding["spec"].get("policyName") != prefix + "-" + suffix or binding["spec"].get(
            "validationActions"
        ) != ["Deny"]:
            raise RuntimeError("Policy binding does not enforce denial: " + suffix)
    ns = read("namespace/" + project_namespace)
    labels = ns["metadata"].get("labels", {})
    if (
        not project_namespace.startswith("mlp-")
        or labels.get("app.kubernetes.io/managed-by") != "mlp-controlplane"
    ):
        raise ValueError("Gate requires an existing owned project namespace")
    base = read("rolebinding/mlp-api-secrets", "-n", project_namespace)
    checked = 0

    def dry_run(body: dict[str, Any], operation: str, allowed: bool, label: str) -> None:
        nonlocal checked
        result = run(operation, "--dry-run=server", "--as", actor, "-f", "-", body=body)
        if allowed:
            if result.returncode:
                raise RuntimeError("Allowed admission operation failed: " + label)
        elif (
            result.returncode == 0
            or "ValidatingAdmissionPolicy" not in result.stderr
            or not re.search(
                r"ValidatingAdmissionPolicy ['\"](?:"
                + "|".join(re.escape(prefix + "-" + suffix) for suffix in POLICIES)
                + r")['\"]",
                result.stderr,
            )
            or "denied" not in result.stderr.lower()
        ):
            # RBAC errors, outages, malformed fixtures and AlreadyExists do not prove denial.
            raise RuntimeError("Expected policy-specific denial was not observed: " + label)
        checked += 1

    dry_run(base, "replace", True, "owned API binding repair")
    for mutation in ("subject", "name", "extra-subject"):
        bad = deepcopy(base)
        if mutation == "subject":
            bad["subjects"][0]["name"] = "attacker"
        elif mutation == "name":
            bad["metadata"] = {
                "name": "admission-check-" + uuid4().hex[:8],
                "namespace": project_namespace,
                "labels": deepcopy(base["metadata"]["labels"]),
            }
        else:
            bad["subjects"].append({"kind": "User", "name": "attacker"})
        dry_run(bad, "create" if mutation == "name" else "replace", False, mutation)
    for namespace in ("kube-system", system_namespace):
        bad = deepcopy(base)
        bad["metadata"] = {
            "name": "admission-check-" + uuid4().hex[:8],
            "namespace": namespace,
            "labels": base["metadata"]["labels"],
        }
        dry_run(bad, "create", False, "foreign RoleBinding " + namespace)
    changed = deepcopy(ns)
    changed["metadata"]["labels"]["mlp.io/project-id"] = str(uuid4())
    dry_run(changed, "replace", False, "ownership replacement")
    foreign = read("namespace/kube-system")
    forged = deepcopy(foreign)
    forged["metadata"]["labels"] = dict(labels)
    dry_run(forged, "replace", False, "foreign namespace label forgery")
    # Creating an unbound ServiceAccount is RBAC-authorized for the reconciler; reject
    # at admission, proving generic foreign workload writes cannot bypass binding checks.
    account = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "admission-check-" + uuid4().hex[:8], "namespace": "kube-system"},
    }
    dry_run(account, "create", False, "foreign provider write")
    return checked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--release", default="mlp")
    parser.add_argument("--system-namespace", default="mlp-system")
    parser.add_argument("--project-namespace", required=True)
    args = parser.parse_args()
    count = verify(args.context, args.release, args.system_namespace, args.project_namespace)
    print(f"PASS: four installed policies, zero type warnings, {count} server dry-run checks")


if __name__ == "__main__":
    main()
