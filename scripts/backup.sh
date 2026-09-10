#!/usr/bin/env bash
# Back up MLflow state: PostgreSQL metadata (pg_dump) and MinIO artifacts.
# Writes to backups/<timestamp>/.
#
# In AWS this is replaced by RDS automated snapshots and S3 versioning; the
# contract it proves — metadata and artifacts restored together stay
# consistent — is the same.
set -euo pipefail

NS="${NAMESPACE:-ml-platform}"
DEST="${1:-backups/$(date +%Y%m%dT%H%M%S)}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"
mkdir -p "$DEST/artifacts"

echo "==> pg_dump -> $DEST/mlflow.sql"
kubectl -n "$NS" exec platform-postgres-0 -- \
  pg_dump -U mlflow -d mlflow --clean --if-exists > "$DEST/mlflow.sql"
echo "    $(wc -l < "$DEST/mlflow.sql" | tr -d ' ') lines"

echo "==> mirror MinIO artifacts (via minio-shell pod)"
kubectl -n "$NS" delete pod minio-shell --ignore-not-found --wait=true >/dev/null 2>&1
kubectl apply -f k8s/jobs/minio-shell.yaml >/dev/null
kubectl -n "$NS" wait --for=condition=ready pod/minio-shell --timeout=120s >/dev/null
# The init container has already mirrored the bucket into /art.
kubectl -n "$NS" cp "minio-shell:art/." "$DEST/artifacts" -c cp >/dev/null
kubectl -n "$NS" delete pod minio-shell --wait=false >/dev/null

objects=$(find "$DEST/artifacts" -type f | wc -l | tr -d ' ')
echo "    $objects artifact files"

cat > "$DEST/MANIFEST" <<EOF
created   : $(date -u +%Y-%m-%dT%H:%M:%SZ)
namespace : $NS
pg_dump   : mlflow.sql
artifacts : artifacts/ ($objects files)
EOF
echo "==> backup complete: $DEST"
