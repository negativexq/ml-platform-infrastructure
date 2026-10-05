# Control-plane lifecycle and budgets

Implemented on 2026-10-05 using local tests; no cluster resources were changed.

## Health and readiness

API and gateway expose unauthenticated `/healthz` (process liveness) and `/readyz`.
Production readiness checks a database connection and `SELECT 1`, then requires the
installed Alembic head set to equal the image's migration heads (currently `0016`).
Failures return 503 without connection details. PostgreSQL connection/pool waits are
bounded to three seconds and readiness statements to two seconds. Chart readiness probes
allow eight seconds; startup/liveness continue to use `/healthz` so a DB outage does not
cause restart loops. Demo readiness has no external database prerequisite.

## Functions

Project credentials and registry access are managed through
[project secrets](secrets.md). Secret values stay in Kubernetes; workload revisions store
only names and key references.

Function registration accepts `requests` and `limits` with both `cpu` and `memory`.
Defaults are `100m`/`128Mi` requested and `1`/`512Mi` limited per replica. Requests must be
positive and no larger than limits. Resource quantities support normal CPU cores/millicores
and the documented byte/SI/binary memory suffixes used by the API validator.

`readiness_path` selects an HTTP probe; omit it for TCP readiness on the function's port.
`readiness_timeout_seconds` defaults to 2 (1–60); `readiness_initial_delay_seconds` defaults
to 0 (0–600). These settings are copied into immutable deployment revisions and restored
on rollback. Old JSON rows receive the defaults when read. The registration form exposes
resources and the HTTP path. Readiness is independent of the workload's request handler.

## Reconciliation retries

The project, job run, pipeline run, deployment and rollout batches isolate provider errors.
A failed entity waits 5, 10, 20, 40, 80, 160, then at most 300 seconds before another attempt;
other entities continue. A successful attempt or optimistic conflict clears the backoff.
Backoff is process-local and resets on restart. The chart still requires one reconciler;
this does not provide distributed ownership or leader election.

## Execution deadlines

Job definitions accept `timeout_seconds` (default 3600, range 1–604800). Starting a job run
can override that default with an optional JSON body. Pipeline-run creation accepts the
same range/default. The chosen value is persisted with each run and compiles into Argo
`activeDeadlineSeconds`; retries keep the original deadline. Reusing an idempotency key
with a different deadline returns a conflict. Migration `0014` gives existing rows the
3600-second default. Argo enforces the deadline; the control plane observes its outcome.

## Deployment deletion

A project admin requests `DELETE /projects/{project}/deployments/{name}` (202), also exposed
on the deployment page. An active canary must be aborted first. The transaction records
`DELETING`, makes the endpoint unavailable and audits the request. Deployment/rollback/
rollout mutations reject deleting/deleted deployments; PostgreSQL row locks serialize
rollout creation and deletion intent. Gateway route caches can retain the previous state
for up to five seconds.

The reconciler idempotently deletes the serving resource and waits for an ABSENT
observation before recording `DELETED` and clearing active/desired revisions. GPU
reservations remain held until that observation. Deleted deployments disappear from lists;
direct lookup retains revision/audit history. Names remain reserved; use a new name for
a replacement. The history is not physically purged.

## Workflow retention

`CP_WORKFLOW_RETENTION_SECONDS=0` disables cleanup by default. Set it, for example, to
`604800` to retain completed workflow/pod logs for seven days. Each pass selects at most
100 completed job runs and 100 completed pipeline runs older than the cutoff. Active runs
are excluded. The reconciler requests Argo foreground deletion of the workflow (owned
pods are garbage collected), records `workflow_cleaned_at` and an audit event. Cleanup
errors back off per entity; a crash between deletion and marking is safe because 404 is
idempotent. Migration `0015` adds the marker and candidate-selection indexes.

The policy applies to existing completed runs too once enabled. Platform run records,
step outcomes, retry relationships, definitions and lineage remain in PostgreSQL. Their
logs endpoint returns 410 after cleanup. Export logs before enabling a short retention;
a durable log archive and physical metadata retention remain separate work.

Foreground resource deletion relies on Kubernetes ownership and finalizers, and needs a
cluster acceptance check. Argo's lifecycle mechanisms are described in its
[official field reference](https://argo-workflows.readthedocs.io/en/release-3.5/fields/).

## LLM admission and settlement

Before forwarding, the gateway atomically reserves capacity in both endpoint and caller
buckets. The prompt estimate counts serialized UTF-8 request bytes (including tools and
schemas) plus 64 units per message; output is bounded by a positive `max_tokens` or
`max_completion_tokens`. If omitted, the output cap is at most 256 and fits the configured
per-minute limit after the prompt estimate. The gateway forwards the cap as `max_tokens`;
multiple completions (`n != 1`) are refused. A request whose estimate cannot fit its
configured limit returns 400; depleted buckets return 429.

Completed responses with valid nonnegative integer usage refund the unused reservation.
Reported usage above the estimate is charged as debt. Missing/malformed usage, oversized
metering input, interrupted streams and upstream connection/timeout failures retain the
full reservation; usage never silently falls back to zero. Cancellation still records
usage. Headers report capacity after reservation, before any refund.

This is a conservative text estimate, not model-specific tokenization or a hard guarantee
on hidden, multimodal or template tokens. Accurate hard token ceilings need a tokenizer
and runtime contract for each model. Limits remain process-local; multiple gateway
replicas multiply available capacity. Shared storage and replica-aware GPU/CPU admission
remain open review items.

See [control-plane recovery](recovery.md) for backup and restore.
