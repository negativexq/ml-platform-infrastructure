# Local release acceptance — 2026-10-07

The digest-pinned slim control-plane and Go S3 initializer were deployed to
`kind-mlp-acceptance`. All seven CPU lifecycle phases passed, including fresh
private MinIO model loading, inference, canary metrics, credential rotation,
drift repair and scale-to-zero/reactivation. Actual KServe initializer images
and terminated exit statuses are recorded in the external archive.

Raw reports/logs stay outside Git. `summary.json` holds metrics; `SHA256SUMS`
verifies the archived files relative to the path in `artifact-location.txt`.
