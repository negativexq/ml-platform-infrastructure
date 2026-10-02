#!/usr/bin/env bash
# Run Keycloak in the kind cluster with the platform's local realm (users alice, bob, carol;
# groups platform-admins, ml-team), then print how to run the control plane against it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

kubectl apply -f k8s/identity/keycloak.yaml >/dev/null
kubectl -n identity create configmap keycloak-realm --from-file=realm-mlp.json=k8s/identity/realm-mlp.json \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n identity rollout restart deployment/keycloak >/dev/null  # re-import the realm file
kubectl -n identity rollout status deployment/keycloak --timeout=5m

cat <<'MSG'

Keycloak is up. In one terminal:
  kubectl -n identity port-forward svc/keycloak 8180:8080

In another, the control plane with sign-in on (the demo shown; `make cp-run` takes the same):
  export CP_AUTH_MODE=oidc CP_OIDC_ISSUER=http://localhost:8180/realms/mlp \
         CP_OIDC_CLIENT_ID=mlp-ui CP_OIDC_CLIENT_SECRET=local-dev-only-ui-secret \
         CP_SESSION_SECRET=$(openssl rand -hex 32) CP_PUBLIC_URL=http://localhost:8080 \
         CP_PLATFORM_ADMINS=group:platform-admins
  make cp-demo        # http://localhost:8080 -> sign in as alice/alice, bob/bob or carol/carol

An API token for a script:
  curl -s -d grant_type=password -d client_id=mlp-cli -d username=bob -d password=bob \
       http://localhost:8180/realms/mlp/protocol/openid-connect/token | jq -r .access_token
MSG
