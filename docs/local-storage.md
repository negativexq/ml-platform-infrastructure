# Local kind storage housekeeping

Local kind nodes use kubelet's native image garbage collection. The profile is in
[`kind-storage-policy.json`](../k8s/kind-storage-policy.json); the creation-time patch
in [`kind-cluster.yaml`](../k8s/kind-cluster.yaml) applies to every node.

| Setting | Value | Behavior |
| --- | --- | --- |
| `imageMaximumGCAge` | `6h` | Collect images unused for six hours, regardless of VM disk usage |
| `imageMinimumGCAge` | `2m` | Minimum age for disk-pressure image collection |
| `imageGCHighThresholdPercent` | `80` | Start image collection above 80% imagefs usage |
| `imageGCLowThresholdPercent` | `70` | Aim to reduce usage to 70%, if enough unused images exist |
| `containerLogMaxSize` | `10Mi` | Rotate each container log at this size |
| `containerLogMaxFiles` | `3` | Keep three files per container |

Kubernetes checks unused images every five minutes. Image age tracking resets when
kubelet restarts, so the six-hour window starts again after a restart. Age-based GC is
enabled by default in Kubernetes 1.30+; the maintenance command rejects older versions
and an explicitly disabled `ImageMaximumGCAge` gate.
See the [Kubernetes 1.32 garbage collection documentation](https://v1-32.docs.kubernetes.io/docs/concepts/architecture/garbage-collection/).

`make kind-up` and `make local-up` also apply the profile to existing running nodes.
Apply it independently, or inspect the effective runtime settings:

```sh
make kind-storage-status CLUSTER_NAME=ml-platform
make kind-storage-apply CLUSTER_NAME=ml-platform

# The acceptance lab has a separate kubeconfig.
KUBECONFIG=/tmp/mlp-acceptance-kubeconfig make kind-storage-status CLUSTER_NAME=mlp-acceptance
KUBECONFIG=/tmp/mlp-acceptance-kubeconfig make kind-storage-apply CLUSTER_NAME=mlp-acceptance
```

The script uses Docker kind labels and an explicit Kubernetes context. It does not start
stopped clusters. Apply preflights every selected running node, preserves unrelated
kubelet settings, saves `/var/lib/kubelet/config.yaml.mlp-storage-backup`, and replaces
the config atomically. It restarts kubelets sequentially and verifies effective settings
through `/configz` and node readiness. An unsuccessful apply restores that node's previous
config. A repeat apply with matching settings does not restart kubelet. Earlier nodes
successfully configured remain configured if a later node fails.
Reapply after a kubeadm upgrade or adding nodes to an existing cluster; the command
does not rewrite kubeadm's shared ConfigMap. Kubelet restarts can briefly affect readiness.

## Scope and host disk pressure

This limits unused node image accumulation and container log retention. It is not an
absolute cap on the total cluster volume. Running images, PostgreSQL, MinIO, etcd,
registry blobs and application artifacts can still consume storage. A scale-to-zero
image may be collected; the next activation will pull it again. Images loaded only with
`kind load` must remain available to reload if they cannot be pulled from a registry.

Docker Desktop's VM sees its own filesystem capacity, which can look mostly empty while
the Mac disk is full. Therefore the 80/70% thresholds alone cannot protect the host;
the age limit is important. `kind-storage-status` reports host free bytes and a warning
below 15 GiB. It is an on-demand check, not an unattended host disk alert or hard quota.

Use `make cache-clean` between large build batches to cap unused Docker build cache and
clear rebuildable tool caches. Registry storage needs a separate retention policy;
database/artifact storage needs deliberate retention and backup. No external periodic
`crictl`/snapshot-directory deletion task is installed; kubelet owns routine node GC.

## Recorded acceptance

On 2026-10-07, the Kubernetes 1.32 ARM64 `mlp-acceptance` node changed from an image GC
high threshold of 100% and maximum age `0s` to this policy. Runtime `/configz` verification
and a repeat apply passed; the repeat reported `changed=false`. PostgreSQL was 1/1 and
API/gateway/reconciler were 2/2 ready after the kubelet restart. See
[`kind-storage-policy.json`](evidence/live-2026-10-07/storage/kind-storage-policy.json).
The natural six-hour age-expiry cycle has not yet been observed; these checks prove the
policy was loaded and the apply is idempotent, not a long-duration GC or log-rotation drill.
