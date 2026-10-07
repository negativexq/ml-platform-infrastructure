# Run Parameters acceptance — 2026-10-08

Immutable Job/Pipeline parameter schemas, resolved run snapshots, retry/idempotency,
per-step projection, schedule date/time bindings and UI controls are implemented.
[Contract](../../../run-parameters.md).

- Clean source snapshot `a8eb333` (full source bundle in external artifacts).
- Final control-plane + dedicated migration release gate: 12 checks passed, head 0023.
- Go gateway: race/vet/build passed; HIGH/CRITICAL vulnerabilities: 0.
- CP fixable HIGH/CRITICAL: 0; all-severity secret findings: 0.
  Unfiltered CP scan retains 55 unfixed Debian findings (53 HIGH, 2 CRITICAL).
- Dependency isolation: no inference/scientific packages; 87 lock packages, 89 runtime
  distributions including pip/project. Trivy unpacked image surface: 452,210,176 bytes.
- New parameter contract tests: 17 passed (memory and real PostgreSQL).
- Browser + secret compatibility tests: 21 passed. Final pipeline/job browser and
  project service rechecks: 21 passed; the literal API-path check passed after correction.
- Broad suite: 924 passed, 13 skipped, 2 existing UI cases deselected; two failures
  investigated. The UI literal-path parser incompatibility was corrected and retested.
  The memory fake concurrent-project race passed the full 18-case project service
  recheck; it is an intermittent fake limitation. Existing role-explanation UI failure
  was reproduced on the previous source snapshot. Existing promotion UI case remains
  outside this change's validation scope.
- Local acceptance Helm revision 31: API/reconciler/gateway/log services all 2/2 ready.
  Real Argo job, retry, pipeline and scheduled pipeline succeeded with frozen values.
  A command-like parameter stayed literal; reusing a key with other values returned 409.
  Fixtures were paused and their projects soft-deleted after verification.

Images:

- CP: `localhost:5201/mlp-controlplane@sha256:9d2ab5675469b648a9b444dab2199e4583d3d939efaac0a10581d8105ccdb6e0`
- Migration: `localhost:5201/mlp-controlplane-migrate@sha256:dcdfaf1470a80dad235989dbeaeedbf61a25ef1c6bd40a267d9c9f5c481a07f1`
- Gateway: `localhost:5201/mlp-gateway-go@sha256:89f4fcd6d53ff221861212a43093c0f0445f73a94ec111ce32d916de94af1182`

Full logs, SBOM, Trivy and live API records remain outside Git at
`/Users/ofk/ml-platform-infra-artifacts/2026-10-08/run-parameters/`.
The checksum manifest identifies those external files. JSON reports are not vendored.
