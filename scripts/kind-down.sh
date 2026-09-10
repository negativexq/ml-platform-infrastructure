#!/usr/bin/env bash
# Tear down the local kind cluster. The compose platform services are left
# running; use `make platform-down` for those.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-ml-platform}"

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  kind delete cluster --name "$CLUSTER_NAME"
else
  echo "cluster '$CLUSTER_NAME' does not exist"
fi
