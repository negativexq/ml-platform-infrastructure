# Control-plane dependency split — 2026-10-07

Inference-only dependencies moved to `.[inference]`. The shared Python control-plane
image retains MLflow skinny, Kubernetes, DB, OIDC, HTTPX and OTel; retained lock versions
and inference/training/serving serialization pins are unchanged. No deployment was made.

| Native Linux ARM64 metric | Before | After |
| --- | ---: | ---: |
| Trivy image size, decimal MB | 838.44 | 448.90 |
| Compressed Docker content, decimal MB | 228.12 | 109.85 |
| Lock packages | 97 | 82 |
| Installed Python distributions | 99 | 84 |
| SBOM packages, including OS packages | 212 | 197 |
| API startup RSS, MiB | 224.63 | 175.68 |
| Reconciler startup RSS, MiB | 213.61 | 162.64 |
| Python rollback gateway startup RSS, MiB | 223.64 | 174.49 |
| Fixable HIGH/CRITICAL / secret findings | 0 / 0 | 0 / 0 |

Image reduction: **46.46%** on the same Trivy size metric. RSS is median PID 1 `VmRSS`
from three samples after readiness and the reconciler's first empty-state pass; it is
an isolated startup measurement, not a production-load benchmark.

Both full release gates passed: migrations to `0021`, API/gateway readiness and
liveness, reconciler initialization, DB outage/recovery, SBOM and scans. The slim image
also passed runtime `pip check` and dependency isolation. Inference build/startup smoke
and ten inference tests passed.

Browser-enabled suite: **849 passed, 13 skipped, 2 UI failures reproduced on baseline**.
Full control-plane mypy retains the same 20 baseline errors; changed tooling passes.
An earlier intermittent memory-adapter race was also reproduced on baseline; the final
suite's corresponding test passed. No state-machine code was changed to mask failures.

Validation used clean temporary local snapshot commits; the main workspace was not
committed. Native AMD64, production OIDC/HA/DB-role checks, a pushed release artifact
and live cluster/model-loading acceptance remain separate checks.

[summary.json](summary.json) retains measurements, image/source identities and checks.
Raw Trivy/SBOM reports, package inventories, logs, JUnit, snapshots and reproduction
scripts are stored **outside the repository**. Their local archive directory is in
[artifact-location.txt](artifact-location.txt); [SHA256SUMS](SHA256SUMS) covers the archived
files. Run `shasum -a 256 -c <absolute-path-to-SHA256SUMS>` from that archive directory
to verify them. The archive's original detailed README preserves the full methodology.
