# Dedicated migration image — 2026-10-07

The standalone migration image uses the same Alembic code and advisory lock.
The full clean-source release gate passed with the dedicated image migrating
the database used by API/gateway/reconciler checks. The local Helm upgrade
then passed its pre-upgrade hook using the pinned migration digest.

Uncompressed Trivy image size: 448,904,192 → 229,410,816 bytes.
Dependency lock: 82 → 14 packages. Fixable HIGH/CRITICAL findings: 0.

Raw reports/logs stay outside Git. `summary.json` holds metrics; `SHA256SUMS`
verifies the archived files relative to the path in `artifact-location.txt`.
