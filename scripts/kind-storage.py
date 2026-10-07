#!/usr/bin/env python3
"""Inspect or enable native kubelet image GC on one explicitly selected kind cluster."""

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "k8s/kind-storage-policy.json").read_text())
CONFIG = "/var/lib/kubelet/config.yaml"


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=20, **kwargs)


def patch_config(original):
    # JSON is also valid YAML. For kind's YAML file replace only known top-level
    # scalar fields; leave authentication, eviction and all other settings intact.
    if original.lstrip().startswith("{"):
        config = json.loads(original)
        config.update(POLICY)
        return json.dumps(config, indent=2) + "\n"
    updated = original.rstrip() + "\n"
    for field, value in POLICY.items():
        pattern = rf"^{field}:.*$"
        line = f"{field}: {json.dumps(value)}"
        if re.search(pattern, updated, re.MULTILINE):
            updated = re.sub(pattern, line, updated, flags=re.MULTILINE)
        else:
            updated += line + "\n"
    return updated


def normalized(value):
    if isinstance(value, str) and re.fullmatch(r"(?:\d+[hms])+", value):
        return sum(
            int(n) * {"h": 3600, "m": 60, "s": 1}[unit]
            for n, unit in re.findall(r"(\d+)([hms])", value)
        )
    return value


def matches(config):
    return all(normalized(config.get(k)) == normalized(v) for k, v in POLICY.items())


def write_config(node, content):
    run(
        [
            "docker",
            "exec",
            "-i",
            node,
            "sh",
            "-c",
            f"cat > {CONFIG}.mlp-tmp && chmod 600 {CONFIG}.mlp-tmp && mv {CONFIG}.mlp-tmp {CONFIG}",
        ],
        input=content,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster", default=os.environ.get("CLUSTER_NAME", "ml-platform"))
    parser.add_argument("--kubeconfig", help="otherwise respects KUBECONFIG")
    parser.add_argument("--apply", action="store_true", help="patch/restart kubelets sequentially")
    parser.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", args.cluster):
        parser.error("invalid kind cluster name")
    kube = ["kubectl", "--context", "kind-" + args.cluster, "--request-timeout=8s"]
    if args.kubeconfig:
        kube += ["--kubeconfig", args.kubeconfig]
    nodes = run(
        [
            "docker",
            "ps",
            "--filter",
            "label=io.x-k8s.kind.cluster=" + args.cluster,
            "--format",
            "{{.Names}}",
        ]
    ).splitlines()
    if not nodes:
        raise RuntimeError("selected kind cluster has no running nodes; no cluster is started")

    def effective(node):
        return json.loads(run(kube + ["get", "--raw", f"/api/v1/nodes/{node}/proxy/configz"]))[
            "kubeletconfig"
        ]

    # Preflight every node before making changes to any node.
    for node in nodes:
        version = run(["docker", "exec", node, "kubelet", "--version"])
        found = re.search(r"v(\d+)\.(\d+)", version)
        if not found or (int(found[1]), int(found[2])) < (1, 30):
            raise RuntimeError("imageMaximumGCAge requires Kubernetes >= 1.30")
        if effective(node).get("featureGates", {}).get("ImageMaximumGCAge") is False:
            raise RuntimeError("ImageMaximumGCAge feature gate is disabled")

    results = []
    for node in nodes:
        current = effective(node)
        changed = False
        if args.apply and not matches(current):
            original = run(["docker", "exec", node, "cat", CONFIG])
            run(
                [
                    "docker",
                    "exec",
                    node,
                    "sh",
                    "-c",
                    f"test -e {CONFIG}.mlp-storage-backup || "
                    f"cp -p {CONFIG} {CONFIG}.mlp-storage-backup",
                ]
            )
            try:
                write_config(node, patch_config(original))
                run(["docker", "exec", node, "systemctl", "restart", "kubelet"])
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    try:
                        current = effective(node)
                        node_state = json.loads(run(kube + ["get", "node", node, "-o", "json"]))
                        ready = any(
                            c["type"] == "Ready" and c["status"] == "True"
                            for c in node_state["status"]["conditions"]
                        )
                        if matches(current) and ready:
                            changed = True
                            break
                    except (subprocess.SubprocessError, json.JSONDecodeError):
                        pass
                    time.sleep(1)
                else:
                    raise RuntimeError("kubelet did not become ready with the requested policy")
            except BaseException:
                write_config(node, original)
                run(["docker", "exec", node, "systemctl", "restart", "kubelet"])
                raise
        results.append(
            {
                "node": node,
                "changed": changed,
                "policy_active": matches(current),
                "effective": {k: current.get(k) for k in POLICY},
            }
        )
    free = shutil.disk_usage(ROOT).free
    report = {
        "cluster": args.cluster,
        "host_free_bytes": free,
        "host_low_space": free < 15 * 1024**3,
        "nodes": results,
    }
    print(json.dumps(report, indent=2))
    if report["host_low_space"] and not args.json:
        print(
            "Host has <15 GiB free: VM imagefs thresholds cannot detect Mac disk pressure. "
            "Use make cache-clean between builds; inspect registry/PVC growth separately."
        )


if __name__ == "__main__":
    main()
