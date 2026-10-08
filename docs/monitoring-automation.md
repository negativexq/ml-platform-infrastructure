# Dataset-triggered monitoring

A project monitoring rule watches future versions of a named observed dataset.
The model version and reference dataset are pinned when the rule is registered.
Register the dataset catalogs first; rule creation validates schemas, credential
references, resource limits and the immutable scientific runtime digest.

Create with `POST /projects/{project}/model-monitoring/rules`; list on the same
path. `PATCH /{rule_id}` accepts `enabled` and the current `revision`. Inspect
`GET /{rule_id}/executions`. The Model Monitoring screen exposes these operations,
exact observed versions, run state and report links. Operators write; viewers read.

Dataset publication and its outbox event commit together. The leader-elected
reconciler dispatches bounded batches using PostgreSQL row locks with
`SKIP LOCKED` and project coordination. A unique `(rule_id, observed_dataset_id)`
intent, immutable job, PENDING run and audit commit together. A restart adopts
committed intent; failed transactions retry without another run. This guarantees
one platform intent/run per occurrence, not exactly-once external side effects.

Feedback is optional. When configured, the observed version must have a
`processing_date`; the latest ground-truth version for that exact date is selected
once and frozen. Until it exists, `WAITING_FEEDBACK` has no job or run. The default
deadline is 24 hours (configurable 60 seconds–7 days); expiry or incompatible data
records a failed monitoring execution without changing producer success.
Pausing suppresses new matches but does not cancel existing waits or runs.
Rules apply to future publications, without historical backfill.

New versions must retain the connections validated at rule creation. Secret key
names (never values) are frozen internally, so the reconciler needs no additional
permission to read project credentials. Kubernetes resolves secrets at pod start,
exactly as for ordinary immutable jobs; forced deletion or incompatible rotation
can fail that independent monitoring run. Public responses exclude this metadata.

The UI and report notifications reuse existing monitoring lifecycle behavior.
Same-DAG dynamic output binding, online prediction capture and Feature Store are
outside this delivery. Per-project rules are bounded at 100; dispatch batches are
bounded at 50, and missing feedback is retried every 15 seconds.

Runtime role provisioning grants the reconciler `INSERT` on immutable
`job_definitions` for generated monitoring checks, without `UPDATE` or `DELETE`.
Existing 0030 deployments created before this grant was added must run role
provisioning again, or apply `GRANT INSERT ON public.job_definitions TO
mlp_reconciler` as the database owner. This permission repair does not require
resubmitting successful batch work; the pending publication event retries.
