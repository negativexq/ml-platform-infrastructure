# Scheduled jobs and pipelines

Schedules are durable platform resources in PostgreSQL. The leader-elected Python
reconciler dispatches them through the same Job/Pipeline run services as manual runs.
Argo remains the workload executor; run history, model discovery and MLflow lineage
continue through the existing run reconcilers.

The UI has global and project **Schedules** pages, with project/target/status filters,
daily/hourly/weekly presets, advanced cron, timezone previews, edit, pause/resume and
execution history. Pipeline details link to their schedules; scheduled runs link back
to the originating execution. Creating/editing requires project operator access;
viewing history requires viewer access. Browser mutations retain CSRF enforcement.

## Contract

- Cron uses five deterministic Unix fields (minute, hour, day, month, weekday), with
  IANA timezones. Random/hash and extended expressions are rejected. As with Unix cron,
  restricted day-of-month and weekday fields use OR semantics.
- DST gaps skip nonexistent wall times. Repeated wall times execute only at the first
  occurrence. Execution identities and stored timestamps use UTC.
- Pipeline versions can be PINNED or LATEST. LATEST resolves when the occurrence intent
  is persisted, including when it enters the queue. Its immutable definition ID and
  timeout/policy snapshot survive edits and restarts. Job definitions are immutable.
- ALLOW ignores the active-run count; FORBID records SKIPPED when busy; QUEUE persists
  only an execution intent until capacity opens. No PENDING platform run exists while
  waiting. PENDING, SUBMITTED and RUNNING scheduled runs count as active.
- SCHEDULE scope considers that schedule's runs. TARGET scope considers scheduled runs
  for the same project and job/pipeline name, across versions and schedules. Manual
  runs are excluded. All scheduled dispatches take the same PostgreSQL target advisory
  transaction lock, including ALLOW and SCHEDULE policies. Policy is evaluated for
  each incoming occurrence; ALLOW can overlap an existing FORBID run.
- QUEUE is bounded (default 100, maximum 1000 intents in the configured scope), ordered
  by scheduled UTC time then execution UUID. Each dispatcher pass considers one head
  per unpaused schedule, preventing one schedule's backlog from filling the batch.
  The queue lifetime is measured from the scheduled time (default one day, maximum
  seven days). Expired intents become MISSED; capacity overflow becomes SKIPPED.
- SKIP records older overdue slots as MISSED when a newer slot is already due; the
  latest slot can start within its deadline. CATCH_UP processes due slots within that
  deadline. Both record slots outside the deadline as MISSED. Defaults: 300-second
  start deadline, at most 20 occurrences per schedule and 50 due schedules per pass.
  Large outages are recovered over multiple passes rather than one unbounded batch.
- Pause stops occurrence creation and queue dispatch. It preserves queued intents and
  does not cancel submitted runs. Resume applies the usual missed-run/queue deadlines.
  Cron/timezone edits reset only the future cursor; existing intents retain their
  definition, revision and policy. Updates require the current `expected_revision`.

## Transaction and recovery

Migration `0022` adds `schedules` and `schedule_executions`. Occurrences have a unique
`(schedule_id, scheduled_for_utc)` key. Database checks prevent a non-DISPATCHED intent
from referencing a run and require DISPATCHED intents to reference exactly one run.
Each platform run can belong to only one execution.

Schedule row locks (`FOR UPDATE SKIP LOCKED`) and target advisory locks coordinate
workers. Occurrence, platform run, pipeline steps, audit events and next cursor commit
in one transaction. The run services expose transaction-aware creation without an
independent commit. Failed transactions roll back all those changes. Committed PENDING
runs are picked up by the existing reconcilers, using deterministic Argo workflow IDs.

The guarantee is one persistent execution intent and at most one platform run per
occurrence. It does not guarantee exactly-once external side effects inside a workload.
Task retries and manual execution still require application-level idempotency.

## API

- `POST/GET /projects/{project}/schedules`
- `GET /schedules?project=...&target_kind=PIPELINE&target_name=...&paused=false`
- `GET/PATCH /schedules/{id}`
- `GET /schedules/{id}/executions`
- `POST /schedule-preview` with `{ "cron": "0 3 * * *", "timezone": "Europe/Istanbul" }`
- `GET /pipeline-runs/{id}/schedule` and `GET /runs/{id}/schedule`

Example creation body:

```json
{
  "name": "daily-credit-scoring",
  "target_kind": "PIPELINE",
  "target_name": "credit-scoring",
  "version_policy": "PINNED",
  "version": 3,
  "cron": "0 3 * * *",
  "timezone": "Europe/Istanbul",
  "concurrency_policy": "QUEUE",
  "concurrency_scope": "TARGET",
  "missed_run_policy": "SKIP"
}
```

Lists/history are paginated. Global lists include only projects the caller can view.
Schedule audit records include the creating actor/configuration and revision changes;
dispatch/run records use the `schedule-dispatcher` actor.

Backfill, replacement/cancellation concurrency, event triggers and first-class batch
inference remain later work. P0 deliberately uses no second orchestration service.

Validation and release scope: [2026-10-08 acceptance](evidence/live-2026-10-08/scheduling/README.md).
