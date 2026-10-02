#!/usr/bin/env bash
# A real Kubernetes control plane without nodes: etcd + kube-apiserver +
# kube-controller-manager, no kubelet and no container runtime.
#
# That is enough to prove everything the control plane does to the Kubernetes
# API (namespaces, RBAC objects, quotas, limit ranges, network policies,
# namespace deletion via the real namespace controller). It deliberately does
# NOT run pods — workload gates (M15+) need a kind cluster — and it does not
# enforce NetworkPolicy (no CNI); enforcement is proven separately in M9.
#
#   scripts/envtest.sh up      start, write $ENVTEST_RUN/kubeconfig
#   scripts/envtest.sh down    stop and delete state
#   scripts/envtest.sh env     print the export line for the tests
set -euo pipefail

K8S_VERSION="${K8S_VERSION:-v1.32.0}"
ETCD_VERSION="${ETCD_VERSION:-v3.5.17}"
BIN="${ENVTEST_BIN:-$HOME/.cache/mlp-envtest/$K8S_VERSION}"
RUN="${ENVTEST_RUN:-/tmp/mlp-envtest}"
API_PORT="${ENVTEST_API_PORT:-16443}"
ETCD_PORT="${ENVTEST_ETCD_PORT:-12379}"
ETCD_PEER_PORT="${ENVTEST_ETCD_PEER_PORT:-12380}"

fetch() {
  mkdir -p "$BIN"
  for b in kube-apiserver kube-controller-manager; do
    [ -x "$BIN/$b" ] || {
      curl -fsSL -o "$BIN/$b" "https://dl.k8s.io/release/$K8S_VERSION/bin/linux/amd64/$b"
      chmod +x "$BIN/$b"
    }
  done
  [ -x "$BIN/etcd" ] || {
    curl -fsSL "https://github.com/etcd-io/etcd/releases/download/$ETCD_VERSION/etcd-$ETCD_VERSION-linux-amd64.tar.gz" \
      | tar -xz -C "$BIN" --strip-components=1 "etcd-$ETCD_VERSION-linux-amd64/etcd"
  }
}

up() {
  fetch
  down quiet
  mkdir -p "$RUN/certs"
  cd "$RUN"
  openssl genrsa -out sa.key 2048 2>/dev/null
  openssl rsa -in sa.key -pubout -out sa.pub 2>/dev/null
  TOKEN="$(openssl rand -hex 16)"
  echo "$TOKEN,admin,admin,system:masters" > tokens.csv

  "$BIN/etcd" --data-dir "$RUN/etcd" \
    --listen-client-urls "http://127.0.0.1:$ETCD_PORT" --advertise-client-urls "http://127.0.0.1:$ETCD_PORT" \
    --listen-peer-urls "http://127.0.0.1:$ETCD_PEER_PORT" >etcd.log 2>&1 &
  echo $! >etcd.pid

  "$BIN/kube-apiserver" \
    --etcd-servers "http://127.0.0.1:$ETCD_PORT" \
    --secure-port "$API_PORT" --bind-address 127.0.0.1 --cert-dir "$RUN/certs" \
    --token-auth-file tokens.csv --authorization-mode RBAC \
    --service-account-issuer https://kubernetes.default.svc \
    --service-account-key-file sa.pub --service-account-signing-key-file sa.key \
    --service-cluster-ip-range 10.0.0.0/24 --disable-admission-plugins ServiceAccount \
    --allow-privileged=true >apiserver.log 2>&1 &
  echo $! >apiserver.pid

  cat >kubeconfig <<K
apiVersion: v1
kind: Config
clusters:
- name: envtest
  cluster: {server: "https://127.0.0.1:$API_PORT", insecure-skip-tls-verify: true}
users:
- name: admin
  user: {token: "$TOKEN"}
contexts:
- name: envtest
  context: {cluster: envtest, user: admin}
current-context: envtest
K
  for _ in $(seq 1 60); do
    curl -fsSk -H "Authorization: Bearer $TOKEN" "https://127.0.0.1:$API_PORT/readyz" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -fsSk -H "Authorization: Bearer $TOKEN" "https://127.0.0.1:$API_PORT/readyz" >/dev/null \
    || { echo "kube-apiserver did not become ready; see $RUN/apiserver.log" >&2; tail -20 apiserver.log >&2; exit 1; }

  "$BIN/kube-controller-manager" --kubeconfig "$RUN/kubeconfig" \
    --controllers=namespace,resourcequota,serviceaccount,serviceaccount-token,garbagecollector \
    --service-account-private-key-file sa.key --use-service-account-credentials=false \
    >controller-manager.log 2>&1 &
  echo $! >controller-manager.pid
  echo "envtest up: export CP_TEST_KUBECONFIG=$RUN/kubeconfig"
}

down() {
  for p in controller-manager apiserver etcd; do
    [ -f "$RUN/$p.pid" ] && kill "$(cat "$RUN/$p.pid")" 2>/dev/null || true
  done
  sleep 1
  [ "${1:-}" = quiet ] || echo "envtest down"
  rm -rf "$RUN"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  env) echo "export CP_TEST_KUBECONFIG=$RUN/kubeconfig" ;;
  *) echo "usage: $0 up|down|env" >&2; exit 2 ;;
esac
