#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
image=${CP_BATCH_IMAGE:-mlp-batch-inference:release-check}
out=${CP_BATCH_EVIDENCE_DIR:?Set CP_BATCH_EVIDENCE_DIR outside the repository}
revision=$(git -C "$root" rev-parse HEAD)
[[ -z "$(git -C "$root" status --porcelain)" ]] || { echo 'Release requires a clean source checkout' >&2; exit 1; }
mkdir -p "$out"
docker build --platform "${CP_BATCH_PLATFORM:-linux/arm64}" --build-arg SOURCE_REVISION="$revision" -t "$image" -f "$root/docker/batch-inference/Dockerfile" "$root"
docker run --rm "$image" python -m pip check
docker run --rm "$image" python -c 'import batch_inference.worker, mlflow.pyfunc, pyarrow.parquet; print("batch runtime imports passed")'
# Run the bounded-worker contract against the actual Linux runtime libraries.
docker run --rm -v "$root/controlplane/tests/test_batch_worker.py:/tests/test_batch_worker.py:ro" "$image" sh -c 'python -m pip install --quiet --no-cache-dir --target=/tmp/test-deps pytest==8.4.2 && PYTHONPATH=/tmp/test-deps:$PYTHONPATH python -m pytest /tests/test_batch_worker.py -q' > "$out/worker-tests.txt"
trivy image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 --format json --output "$out/trivy-fixable.json" "$image"
trivy image --scanners vuln --severity HIGH,CRITICAL --format json --output "$out/trivy-full.json" "$image"
trivy image --scanners secret --exit-code 1 --format json --output "$out/secrets.json" "$image"
trivy image --format cyclonedx --output "$out/sbom.cdx.json" "$image"
docker image inspect "$image" --format '{{json .}}' > "$out/image.json"
echo "Batch inference release checks passed: $image ($revision)"
