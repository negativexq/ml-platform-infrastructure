# Managed Batch Inference — local acceptance, 2026-10-08

Scope: one S3/MinIO CSV or Parquet object and a pinned classic MLflow model version.
Managed definitions reuse Job/Pipeline runs and JOB schedules; successful output dataset,
run/step CAS and lineage audit commit atomically. See [contract](../../../batch-inference.md).

- Final backend suite: **816 passed, 13 skipped** (existing optional integration checks).
- Focused parameter/schedule/batch UI and contracts: **40 passed**. Final worker/UI changes:
  **10 passed**; Linux image worker contract: **9 passed**. Frontend: build + **22 tests**.
- Control-plane clean source `560ee6c777d68a502295b6a20834388e9ace5549`:
  **all 12 release gates passed**, including migration to `0025`, dependency isolation,
  outage/recovery, SPDX, fixable HIGH/CRITICAL and all-severity secret gates.
- Batch clean source `b98f23c9705f2f7e2b2ab462c1a27bf9dbd52a5c`: pip check, runtime imports,
  Linux worker tests, fixable HIGH/CRITICAL, all-severity secret scan and CycloneDX passed.
  This source separates application and dependency Docker layers; control-plane/Gateway
  source files are identical to their validated source snapshot (323 files compared).
- Go Gateway race/vet/build and **unfiltered HIGH/CRITICAL = 0** passed. Additive head `0025`
  is accepted; gateway rolled first. Final Helm revision **36**, API/Gateway/reconciler/logs 2/2.
- Real MinIO objects and MLflow registry version, not mocked storage: **CSV → Parquet** and
  **Parquet → CSV** Ridge predictions match expected values and SHA-256; identifiers preserved.
  Nine rows in batches of two verify chunk boundaries, not production-scale throughput.
- Real Argo job, retry, pipeline step and recurring JOB schedule succeed. Retry has a different
  object and dataset version; pipeline output has pipeline lineage and step identity. Corrupt
  SHA-256 fails without a catalog output. Transaction crash/RBAC tests cover rollback and isolation.
- Browser captures verify creation/start, output versions and timestamp/level/source log rows.
  Stage/progress logs are concise; progress emits at most every ten seconds. One dependency
  warning from the test export's inferred optional `psutil` requirement remains visible; warnings
  are not suppressed or dependencies installed by the worker. Predictions still passed.

Trivy uncompressed artifact size: control-plane **452,366,336 bytes**, batch **824,998,400 bytes**.
Control-plane stays at **89 installed distributions**; the scientific/Arrow stack is isolated in
batch runtime. Each Python image has **0 fixable HIGH/CRITICAL and 0 secrets**. Full scans retain
**55 unfixed Debian findings** (53 HIGH, 2 CRITICAL); no claim of an empty full vulnerability scan.

Own temporary projects, registry models and S3 objects removed after capturing synthetic outputs.
Unused registry manifests/node images/host images and build cache cleaned; host has **3 referenced
images and zero build cache**, approximately **13 GiB free**. Data volumes were retained. Current
control-plane, migration, gateway and configured batch image manifests returned HTTP 200 after GC.
The first 2 GiB acceptance request exceeded remaining cluster allocation; it was cancelled, and
small fixtures ran with 1 GiB through the supported resource setting.

Full JSON/SBOM/scans/screenshots/test logs/source bundles and small synthetic outputs stay outside
Git at `/Users/ofk/ml-platform-infra-artifacts/2026-10-08/batch-inference/`.
[ARTIFACTS.sha256](ARTIFACTS.sha256) identifies those artifacts. Only this summary and checksums are
tracked. Initial/intermediate build reports are retained outside Git; the final deployment uses
`registry-active-checks.json`. This verifies the selected local acceptance cluster, not another
production environment. Multi-object fan-out and dynamically selected input versions are deferred.
