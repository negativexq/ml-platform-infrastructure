#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
image=${CP_GATEWAY_GO_IMAGE:-mlp-gateway-go:release-check}
platform=${CP_GATEWAY_GO_PLATFORM:-linux/arm64}
out=${CP_GATEWAY_GO_EVIDENCE_DIR:-"$root/docs/evidence/gateway-go-release"}
mkdir -p "$out"
(cd "$root/services/gateway-go"; go test -race ./...; go vet ./...)
docker build --platform "$platform" -t "$image" "$root/services/gateway-go"
trivy image --scanners vuln --severity HIGH,CRITICAL --exit-code 1 --format json --output "$out/trivy.json" "$image"
trivy image --format cyclonedx --output "$out/sbom.cdx.json" "$image"
docker image inspect "$image" --format '{{json .}}' > "$out/image.json"
printf 'Gateway Go release checks passed: %s (%s)\n' "$image" "$platform"
