# PostgreSQL limiter follow-up — 2026-10-07

Recorded local date: Europe/Istanbul. Single ARM64 Kubernetes 1.32 kind lab;
`kind-mlp-acceptance`; auth mode `none`. These are ten-second admission/rejection
samples with a 600/minute endpoint and caller budget, not 500 successful inference RPS.

**Two/four gateway targets passed 500 offered RPS. The complete 1/2/4 matrix is still
failed at one gateway, and the 2 CPU single-gateway probe still queues.**

- [Summary and cleanup](summary.json): before/after metrics and the narrowed passing scope.
- [Baseline](baseline.json) and [optimized](optimized.json): identical aiohttp/uvloop
  sampling code and generator image; four gateway pods running, 1/2/4 distinct pod IP
  targets, 1 CPU per gateway. Each sample has shared-budget checks and isolated OTLP
  histogram deltas, HTTP/client queue latency, scheduling lag and generator CPU data.
- [Single-gateway 2 CPU probe](single-replica-capacity.json): the initial throughput-only
  gate reported a pass, but retained 2.2s p95 client queue. The corrected gate rejects it;
  its legacy result is preserved. Queue/scheduling guards are fixture guards, not
  production inference latency SLOs.
- [HTTPX baseline](httpx-baseline.json) and [HTTPX optimized](httpx-optimized.json):
  diagnostic attempts with CPU-heavy client queueing, retained separately.
- [Baseline gateway CPU](baseline-gateway-cpu.json): cumulative post-matrix observations,
  not isolated per-sample CPU measurements.
- [Image provenance/checks](image.json), [Trivy](trivy.json), [SPDX SBOM](sbom.spdx.json)
  and [acceptance build recipe](acceptance-image.Dockerfile): working-tree artifact,
  verified application wheel/source hash, `pip check`, zero fixable HIGH/CRITICAL;
  full clean release and AMD64 runtime remain separate gates. The recipe's base tag
  maps to the immutable dependency-base digest recorded in image.json.

Read the [full report and repeat procedure](../../../limiter-performance.md).
No builds/scans/pushes/deployments/disk cleanup overlapped the measured aiohttp matrices.
The preceding disk incident/restart is described in summary.json and is not a recovery
backup/restore drill. All PVCs were retained; the unrelated lab node was stopped again.
