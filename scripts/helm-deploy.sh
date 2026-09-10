#!/usr/bin/env bash
# Install or upgrade the inference release with Helm.
#
#   ./scripts/helm-deploy.sh                    # values-local.yaml
#   ./scripts/helm-deploy.sh --set image.tag=v2 # override anything
set -euo pipefail

RELEASE="${RELEASE:-inference}"
NAMESPACE="${NAMESPACE:-ml-platform}"
CHART="${CHART:-helm/ml-platform}"
VALUES="${VALUES:-helm/ml-platform/values-local.yaml}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"

# The kind bridge gateway is machine-specific, so it is discovered rather than
# committed. This is the one value the chart cannot know on its own.
PLATFORM_HOST="$(docker network inspect kind -f '{{json .IPAM.Config}}' \
  | python3 -c 'import json,sys,ipaddress
for cfg in json.load(sys.stdin):
    gw = cfg.get("Gateway", "")
    if gw and isinstance(ipaddress.ip_address(gw), ipaddress.IPv4Address):
        print(gw); break')"
echo "platform host: $PLATFORM_HOST"

helm upgrade --install "$RELEASE" "$CHART" \
  --namespace "$NAMESPACE" --create-namespace \
  --values "$VALUES" \
  --set "hostAliases[0].ip=${PLATFORM_HOST}" \
  --set "hostAliases[0].hostnames[0]=platform-host" \
  --wait --timeout 5m \
  "$@"

helm -n "$NAMESPACE" list --filter "^${RELEASE}$"
