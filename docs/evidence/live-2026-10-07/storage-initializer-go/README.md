# Go initializer acceptance — 2026-10-07

The S3-only initializer uses AWS SDK Go v2 in a scratch image, retaining KServe CLI
arguments and UID 1000. Python and its multi-cloud dependency set were removed.

| Native Linux ARM64 size metric | Bytes | Decimal MB |
| --- | ---: | ---: |
| Go runtime size reported by Trivy | 9,528,320 | 9.53 |
| Go compressed Docker content | 3,662,833 | 3.66 |
| Previous Python runtime size reported by Trivy | 301,766,144 | 301.77 |

Runtime-size reduction: **96.8%**. Docker total disk usage was 13.2 MB, a separate metric.
SBOM: 21 packages. Scan: zero fixable HIGH/CRITICAL vulnerabilities and zero secrets.

Race/vet/module checks, six bootstrap tests, ARM64/AMD64 builds and disposable MinIO
prefix/exact-object/missing-prefix/size-limit checks passed. AMD64 runtime was emulated.
SDK tests cover pagination, credentials, checksums and custom CA/anonymous TLS.

This is a local working-tree artifact, not a clean pushed release. No fresh complete
KServe lifecycle, real-model inference, native AMD64 or AWS IRSA acceptance is claimed.
Publication is atomic per top-level entry; successful init completion gates serving.

[summary.json](summary.json) retains measurements, image identity and source hashes.
Raw scan/SBOM, image metadata, tests and reproduction script are archived **outside the
repository**. See [artifact-location.txt](artifact-location.txt) and [SHA256SUMS](SHA256SUMS).
Run `shasum -a 256 -c <absolute-path-to-SHA256SUMS>` from the archive directory to verify
its files. The archived original README contains the detailed methodology.
