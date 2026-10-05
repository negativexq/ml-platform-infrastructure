"""Evaluate the chart's actual CEL, including direct compromised-SA request fixtures."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml  # type: ignore[import-untyped]

from controlplane.adapters.kubernetes.provisioner import KubernetesClusterProvider
from controlplane.application.namespaces import namespace_spec
from controlplane.domain.entities import Project

ROOT = Path(__file__).resolve().parents[2]
SYSTEM = "mlp-system"
NAME = "test-controlplane"
PREFIX = f"{SYSTEM}-{NAME}"
ACTOR = f"system:serviceaccount:{SYSTEM}:{NAME}-reconciler"


@pytest.mark.skipif(
    not shutil.which("helm") or not shutil.which("go"), reason="Helm and Go required"
)
def test_rendered_admission_cel_allows_provisioning_and_denies_escape() -> None:
    rendered = subprocess.run(
        ["helm", "template", "test", str(ROOT / "helm/controlplane"), "-n", SYSTEM],
        capture_output=True,
        text=True,
        check=True,
    )
    docs = [d for d in yaml.safe_load_all(rendered.stdout) if d]
    policies = {
        d["metadata"]["name"].removeprefix(PREFIX + "-"): d["spec"]
        for d in docs
        if d["kind"] == "ValidatingAdmissionPolicy"
    }
    assert set(policies) == {"namespace-owner", "project-writes", "project-rbac", "workflow-role"}
    bindings = [d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding"]
    assert len(bindings) == 4
    assert {b["spec"]["policyName"] for b in bindings} == {PREFIX + "-" + p for p in policies}
    assert all(b["spec"]["validationActions"] == ["Deny"] for b in bindings)
    for policy in policies.values():
        assert policy["failurePolicy"] == "Fail"
        assert "namespaceSelector" not in policy["matchConstraints"]
        assert "objectSelector" not in policy["matchConstraints"]
        assert policy["matchConstraints"]["resourceRules"][0]["operations"] == [
            "CREATE",
            "UPDATE",
            "DELETE",
        ]
    fixtures: list[dict[str, Any]] = []
    project = Project.create(name="team", display_name=None, description="", now=datetime.now(UTC))
    spec = namespace_spec(project)
    ns: dict[str, Any] = {"metadata": {"name": spec.namespace, "labels": dict(spec.labels)}}
    provider = KubernetesClusterProvider(
        None,
        api_service_account=NAME + "-api",
        api_namespace=SYSTEM,
        secret_cluster_role=PREFIX + "-project-secrets",
        workload_cluster_role=PREFIX + "-project-workload-reader",
    )
    desired = provider._desired(spec)

    def case(
        label: str,
        policy: str,
        target: dict[str, Any],
        allowed: bool,
        *,
        operation: str = "CREATE",
        old: dict[str, Any] | None = None,
        namespace: dict[str, Any] | None = ns,
        resource: str = "rolebindings",
        group: str = "rbac.authorization.k8s.io",
        actor: str = ACTOR,
    ) -> None:
        fixtures.append(
            {
                "name": label,
                "policy": policies[policy],
                "allowed": allowed,
                "input": {
                    "object": target if operation != "DELETE" else None,
                    "oldObject": (old or target) if operation != "CREATE" else None,
                    "namespaceObject": namespace,
                    "request": {
                        "operation": operation,
                        "userInfo": {"username": actor},
                        "namespace": namespace["metadata"]["name"] if namespace else "",
                        "resource": {"group": group, "resource": resource},
                    },
                },
            }
        )

    for binding_kind in ("secretrolebinding", "workloadrolebinding", "rolebinding"):
        binding = desired[binding_kind]
        for operation in ("CREATE", "UPDATE", "DELETE"):
            case(f"{binding_kind}-{operation}", "project-rbac", binding, True, operation=operation)
            case(
                f"{binding_kind}-{operation}-scope",
                "project-writes",
                binding,
                True,
                operation=operation,
            )
        for mutation in (
            "name",
            "subject",
            "subject-namespace",
            "extra-subject",
            "subject-kind",
            "role",
            "role-kind",
            "labels",
        ):
            bad = deepcopy(binding)
            if mutation == "name":
                bad["metadata"]["name"] = "attacker-binding"
            elif mutation == "subject":
                bad["subjects"][0]["name"] = "attacker"
            elif mutation == "subject-namespace":
                bad["subjects"][0]["namespace"] = "foreign"
            elif mutation == "extra-subject":
                bad["subjects"].append({"kind": "User", "name": "attacker"})
            elif mutation == "subject-kind":
                bad["subjects"][0]["kind"] = "User"
            elif mutation == "role":
                bad["roleRef"]["name"] = "cluster-admin"
            elif mutation == "role-kind":
                bad["roleRef"]["kind"] = (
                    "Role" if binding["roleRef"]["kind"] == "ClusterRole" else "ClusterRole"
                )
            else:
                bad["metadata"]["labels"]["mlp.io/project-id"] = str(uuid4())
            for operation in ("CREATE", "UPDATE", "DELETE"):
                case(
                    f"{binding_kind}-{mutation}-{operation}",
                    "project-rbac",
                    bad,
                    False,
                    operation=operation,
                )
        repaired = deepcopy(binding)
        corrupt = deepcopy(binding)
        corrupt["subjects"][0]["name"] = "attacker"
        case(
            binding_kind + "-repair",
            "project-rbac",
            repaired,
            True,
            operation="UPDATE",
            old=corrupt,
        )

    for operation in ("CREATE", "UPDATE", "DELETE"):
        case(
            "owned-ns-" + operation,
            "namespace-owner",
            ns,
            True,
            operation=operation,
            namespace=None,
            resource="namespaces",
            group="",
        )
        for foreign_name in ("kube-system", "foreign", "mlp-unowned"):
            foreign = {"metadata": {"name": foreign_name}}
            case(
                f"foreign-ns-{foreign_name}-{operation}",
                "namespace-owner",
                foreign,
                False,
                operation=operation,
                namespace=None,
                resource="namespaces",
                group="",
            )
            for resource, group in (
                ("rolebindings", "rbac.authorization.k8s.io"),
                ("workflows", "argoproj.io"),
                ("inferenceservices", "serving.kserve.io"),
                ("serviceaccounts", ""),
            ):
                case(
                    f"foreign-write-{foreign_name}-{resource}-{operation}",
                    "project-writes",
                    desired["secretrolebinding"],
                    False,
                    operation=operation,
                    namespace=foreign,
                    resource=resource,
                    group=group,
                )
    foreign_old = {"metadata": {"name": spec.namespace, "labels": {}}}
    case(
        "adopt-foreign-namespace",
        "namespace-owner",
        ns,
        False,
        operation="UPDATE",
        old=foreign_old,
        namespace=None,
        resource="namespaces",
        group="",
    )
    for key in ("app.kubernetes.io/managed-by", "mlp.io/project-id", "mlp.io/project"):
        changed = deepcopy(ns)
        changed["metadata"]["labels"][key] = (
            "mlp-controlplane" if key.endswith("managed-by") else str(uuid4())
        )
        if key.endswith("managed-by"):
            changed["metadata"]["labels"][key] = "attacker"
        case(
            "change-ownership-" + key,
            "namespace-owner",
            changed,
            False,
            operation="UPDATE",
            old=ns,
            namespace=None,
            resource="namespaces",
            group="",
        )
        removed = deepcopy(ns)
        del removed["metadata"]["labels"][key]
        case(
            "remove-ownership-" + key,
            "namespace-owner",
            removed,
            False,
            operation="UPDATE",
            old=ns,
            namespace=None,
            resource="namespaces",
            group="",
        )
    for mode in ("enforce", "warn", "audit"):
        for value in ("baseline", "privileged", None):
            weakened = deepcopy(ns)
            key = "pod-security.kubernetes.io/" + mode
            if value is None:
                del weakened["metadata"]["labels"][key]
            else:
                weakened["metadata"]["labels"][key] = value
            case(
                f"weaken-PSA-{mode}-{value}",
                "namespace-owner",
                weakened,
                False,
                operation="UPDATE",
                old=ns,
                namespace=None,
                resource="namespaces",
                group="",
            )
        stale = deepcopy(ns)
        stale["metadata"]["labels"][f"pod-security.kubernetes.io/{mode}-version"] = "v1.24"
        case(
            f"weaken-PSA-version-{mode}",
            "namespace-owner",
            stale,
            False,
            operation="UPDATE",
            old=ns,
            namespace=None,
            resource="namespaces",
            group="",
        )
    legacy = deepcopy(desired["rolebinding"])
    legacy["subjects"][0]["name"] = "mlp-workload"
    case("deny-legacy-shared-SA-binding", "project-rbac", legacy, False)
    unrelated = deepcopy(ns)
    unrelated["metadata"]["labels"]["example.test/info"] = "new"
    case(
        "repair-unrelated-label",
        "namespace-owner",
        unrelated,
        True,
        operation="UPDATE",
        old=ns,
        namespace=None,
        resource="namespaces",
        group="",
    )
    forged = deepcopy(ns)
    forged["metadata"]["name"] = "kube-system"
    case(
        "forged-foreign-labels",
        "project-writes",
        desired["secretrolebinding"],
        False,
        namespace=forged,
    )
    role = desired["role"]
    for operation in ("CREATE", "UPDATE", "DELETE"):
        case(
            "workflow-role-" + operation,
            "workflow-role",
            role,
            True,
            operation=operation,
            resource="roles",
        )
    for mutation in ("name", "verbs", "resources", "extra-rule"):
        bad = deepcopy(role)
        if mutation == "name":
            bad["metadata"]["name"] = "attacker-role"
        elif mutation == "extra-rule":
            bad["rules"].append(deepcopy(bad["rules"][0]))
        else:
            bad["rules"][0][mutation].append("*")
        case("bad-workflow-role-" + mutation, "workflow-role", bad, False, resource="roles")
    lease = {"metadata": {"name": NAME + "-reconciler"}}
    system_ns = {"metadata": {"name": SYSTEM}}
    for operation in ("CREATE", "UPDATE", "DELETE"):
        case(
            "control-lease-" + operation,
            "project-writes",
            lease,
            operation != "DELETE",
            operation=operation,
            namespace=system_ns,
            resource="leases",
            group="coordination.k8s.io",
        )
    case(
        "wrong-control-lease",
        "project-writes",
        {"metadata": {"name": "other-lease"}},
        False,
        namespace=system_ns,
        resource="leases",
        group="coordination.k8s.io",
    )
    for policy in policies:
        case("trusted-operator-" + policy, policy, {}, True, namespace=None, actor="operator")
    env = {**os.environ, "GOMAXPROCS": "2"}
    result = subprocess.run(
        ["go", "run", "-mod=readonly", "."],
        cwd=ROOT / "scripts/admission-cel",
        input=json.dumps(fixtures),
        capture_output=True,
        text=True,
        env=env,
        timeout=240,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    print(result.stdout.strip())


def test_chart_rejects_kubernetes_without_stable_admission_api() -> None:
    if not shutil.which("helm"):
        pytest.skip("Helm required")
    result = subprocess.run(
        ["helm", "template", "test", str(ROOT / "helm/controlplane"), "--kube-version", "1.29.0"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "kubeVersion" in result.stderr
