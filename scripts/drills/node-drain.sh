#!/usr/bin/env bash
# M10 drill — drain a node carrying an inference pod and prove the PDB does
# its job: the Service never drops below minAvailable while the drain is
# honoured.
set -euo pipefail

NS="${NAMESPACE:-ml-platform}"

# Find a node actually running an inference pod.
NODE=$(kubectl -n "$NS" get pods -l app.kubernetes.io/component=inference \
  -o jsonpath='{.items[0].spec.nodeName}')
echo "draining node: $NODE"

echo
echo "=== before ==="
kubectl -n "$NS" get pods -l app.kubernetes.io/component=inference -o wide
kubectl -n "$NS" get pdb ml-platform-inference

echo
echo "=== cordon + drain (background availability probe running) ==="
rm -f /tmp/drain-codes.txt
( for _ in $(seq 1 60); do
    curl -s -o /dev/null -w '%{http_code}\n' --max-time 2 http://localhost:30080/predict \
      -X POST -H 'content-type: application/json' -d '{"features":[1,2,3]}' >> /tmp/drain-codes.txt
    sleep 1
  done ) &
PROBE_PID=$!

start=$(date +%s)
kubectl drain "$NODE" --ignore-daemonsets --delete-emptydir-data --timeout=120s
echo "drained in $(( $(date +%s) - start ))s"

wait "$PROBE_PID" || true

echo
echo "=== after ==="
kubectl -n "$NS" get pods -l app.kubernetes.io/component=inference -o wide
kubectl -n "$NS" get nodes

echo
echo "=== request outcomes during the drain ==="
sort /tmp/drain-codes.txt | uniq -c

echo
echo "=== uncordon ==="
kubectl uncordon "$NODE"
kubectl -n "$NS" get nodes
