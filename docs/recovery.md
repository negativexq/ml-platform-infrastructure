# Control-plane backup and restore

Prepared on 2026-10-05. The scripts have local safeguard tests; no live backup, restore,
database process, container or cluster drill was run for this change. RPO/RTO are unmeasured.

`scripts/controlplane_backup.py` uses installed `psql`, `pg_dump` and `pg_restore` from a
PostgreSQL version compatible with the server. Supply connection parameters through libpq
`PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and `PGPASSFILE` (mode 0600), plus the appropriate
TLS settings. `PGDATABASE` must be a dedicated database name. Do not pass a SQLAlchemy URL
or place passwords on the command line. Set `PGCONNECT_TIMEOUT` for the recovery environment.

## Backup

```bash
export PGHOST=db.example.internal PGPORT=5432 PGDATABASE=controlplane PGUSER=backup_user
export PGPASSFILE=/secure/controlplane.pgpass PGSSLMODE=verify-full PGCONNECT_TIMEOUT=5
python3 scripts/controlplane_backup.py backup /secure/backups/controlplane-20261005
```

The destination must not exist. Its permissions are 0700; the custom-format dump and
manifest are 0600. An exporting read-only repeatable-read transaction stays open while
`pg_dump --snapshot` and the metadata query use the same snapshot. The manifest records
schema heads, counts for all 18 durable tables, SHA-256 and UTC timestamp. A failed backup
has no completed manifest. Upload/encrypt the complete directory with your backup system,
restrict access, and define scheduled frequency and retention to match the chosen RPO.
A dump includes access state and audit data; the script does not print credentials or
raw libpq errors.

## Restore to a new database

First validate the archive without contacting a target database:

```bash
python3 scripts/controlplane_backup.py restore /secure/backups/controlplane-20261005
```

Create a dedicated empty recovery database with the same required extensions and a role
able to own the restored schema. Set the libpq variables to that database, then execute:

```bash
export PGDATABASE=controlplane_recovery
python3 scripts/controlplane_backup.py restore /secure/backups/controlplane-20261005 --execute
```

The script rejects targets with user tables/views/sequences. It does not drop or clean a
target. `pg_restore` uses `--single-transaction --exit-on-error --no-owner --no-privileges`.
After restore it compares schema heads and all durable table counts with the manifest.
A post-restore mismatch fails the command; keep that database isolated and investigate.
Use an image matching the backup schema first, or explicitly migrate the recovered
copy before checking the current image's `/readyz`. Do not point live clients at a partially
verified restore.

## Acceptance drill (pending)

1. Record PostgreSQL/client/image versions, backup timestamp, archive size and time to
   backup/restore. Keep the original database untouched.
2. Restore to an isolated database and run the schema/count checks above. Confirm projects,
   memberships, key revocation/expiry, deployment revisions, run/step lineage and audit
   history against known fixture records. Confirm foreign-key constraints are installed.
3. Start API/gateway with the recovered database in an isolated environment. Check
   `/readyz`, sign-in/project roles, rejection of a revoked key, deployment history, retries
   and expired log responses. Prevent external writes during this phase.
4. Before switching production, stop writers (API/gateway/reconciler), identify external
   resources created since the snapshot, and agree how to handle them. Then switch all
   processes together; do not run old and restored reconcilers concurrently.
5. Resume the single reconciler, verify observed serving/workflow identities and confirm
   idempotent convergence. Measure elapsed recovery time and last recoverable data point;
   record actual RTO/RPO and any repairs in `local-verification.md`.

This dump covers the control-plane database only. MLflow database/artifacts, MinIO/S3
objects, PostgreSQL roles, Kubernetes resources, Secrets, OIDC identity and durable logs
need their own backups. Snapshot consistency does not provide a distributed snapshot
across those systems. Existing [MLflow backup](../scripts/backup.sh) remains separate.
