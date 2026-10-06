# ARM64 serving verification

This is an isolated ARM64 acceptance fixture, built from a dirty working tree with
`b7a3420-working-tree` image labels. It is not a clean production release artifact.

- [Local runtime](local-runtime.json): actual training artifact loads on native ARM64;
  readiness, MLflow JSON prediction and native V2 prediction pass.
- [Deployed source check](controlplane-source.json): the first deployed adapter fixes
  match tested working-tree bytes. The subsequent gateway fix is deployed in the
  image digest recorded in the gateway prediction report.
- [CPU partial report](cpu-lifecycle-storage-ref.json): real project/RBAC, training,
  MLflow registration/discovery and evaluation pass. KServe then reaches READY with
  the revision-specific storage account; this historical attempt failed with gateway 422 because its
  HTTP adapter forwarded `instances` to the tensor-only V2 route.
- The S3 storage Secret reference was missing from the acceptance fixture's model;
  the script now includes it. This was a harness error, not a reason to weaken auth.
- [Runtime manifest](mlflow-runtime.yaml) selects the native image for MLflow only.
  [Initializer manifest](s3-storage-initializer.yaml) handles classic S3 only; S3 is
  removed from the upstream default's URI list so selection is unambiguous. Other
  providers still use upstream containers and have no ARM64 acceptance proof.

Security release gate is **blocked**: [serving scan](trivy-serving.json) reports four
fixable HIGH findings (cryptography plus Starlette); [initializer scan](trivy-initializer-current.json)
reports one protobuf finding. No CVE exclusions were added. Upstream dependency bounds
prevent a simple lock update: MLServer caps FastAPI and full MLflow caps cryptography;
KServe 0.15's Python SDK caps protobuf below the fixed version. These images prove lab
architecture/runtime behavior and must not be presented as production-ready.
[Serving SBOM](sbom-serving.spdx.json) is retained. AMD64 dependency resolution matches
ARM64 versions; an AMD64 image runtime/build gate has not been run.

Gateway routing is now fixed in source through a shared helper: MLflow `instances`
uses `/invocations`, native tensor `inputs` uses `/v2/models/<name>/infer`. The gateway,
HTTP and KServe suites passed 58 tests; Ruff and mypy (five changed source files) passed.
[Actual gateway prediction](gateway-prediction.json) now passes after rollout:
HTTP 200 and the expected `-0.9069787623077955` result from the real trained artifact.
This verifies project → training/discovery → serving → gateway, but does not make
the interrupted CPU report a full canary/rollback/cold-start acceptance pass.
