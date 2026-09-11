#!/usr/bin/env bash
# M10 drill — drive load with k6 while sampling HPA state, and prove scale-up
# and scale-down both happen with real numbers attached.
set -euo pipefail

NS="${NAMESPACE:-ml-platform}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

sample() {
  local t="$1"
  local hpa deploy
  hpa=$(kubectl -n "$NS" get hpa ml-platform-inference -o jsonpath='{.status.currentCPUUtilizationPercentage}' 2>/dev/null || echo '?')
  deploy=$(kubectl -n "$NS" get deploy ml-platform-inference -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo '?')
  echo "t+${t}s  cpu=${hpa:-?}%  readyReplicas=${deploy:-?}"
}

echo "=== baseline ==="
sample 0

echo
echo "=== starting k6 load (60 VUs, ~4 minutes) in the background ==="
k6 run --quiet scripts/loadtest/predict.js > /tmp/k6-autoscale.log 2>&1 &
K6_PID=$!

start=$(date +%s)
peak=2
for i in $(seq 1 32); do
  sleep 10
  el=$(( $(date +%s) - start ))
  sample "$el"
  r=$(kubectl -n "$NS" get deploy ml-platform-inference -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
  [ "${r:-0}" -gt "$peak" ] 2>/dev/null && peak=$r
done

wait $K6_PID || true
echo
echo "=== k6 summary ==="
tail -25 /tmp/k6-autoscale.log

echo
echo "=== scale-down (stabilization window, sampled every 15s) ==="
start=$(date +%s)
for i in $(seq 1 16); do
  sleep 15
  el=$(( $(date +%s) - start ))
  sample "$el"
  r=$(kubectl -n "$NS" get deploy ml-platform-inference -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)
  [ "${r:-0}" = "2" ] && [ $i -gt 2 ] && break
done

echo
echo "peak ready replicas observed: $peak"
kubectl -n "$NS" get hpa ml-platform-inference
