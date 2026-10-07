# Go log streaming

Job run and pipeline step pages use a dedicated Go service for live Kubernetes pod logs.
The existing text endpoints remain available for finished runs and deployments that have
not enabled streaming. Reconciler state machines remain Python.

The browser asks the API for a ticket using its normal session and CSRF header:

```text
POST /runs/{id}/logs/stream-ticket
POST /pipeline-runs/{id}/steps/{step}/logs/stream-ticket
```

Project viewers can read logs; invokers and users outside the project cannot. The API resolves
the workload through its stored workflow reference and signs the namespace, pod UID,
workflow UID and `main` container. Retention-expired workflows return 410. Pending pods
return 409. Tickets are admitted for 60 seconds and streams last at most five minutes.

The browser then uses `fetch` with the ticket in an Authorization header to `/log-stream`.
Cookies, URL tokens and persistent browser storage are not used for the Go request. Route
changes abort the connection. The UI keeps at most 1 MiB of text, renders it as text, and
reconnects with a new ticket after a timeout or transient disconnect. Completed runs use
the existing snapshot endpoint. The CSP remains `connect-src 'self'`: route `/log-stream`
through the same public origin as the API/UI. An absolute publicURL must use that origin.

The viewer uses one event layout: time, level, source and a short message. It understands
JSON/structlog records, Python-style log headers and ordinary stdout. Consecutive identical
events are counted, long diagnostics and tracebacks are collapsed into Details, and JSON
fields are expandable. WARN remains WARN even when its explanatory text mentions errors.
Argo logfmt output uses its explicit severity, so `error="<nil>"` on an INFO process exit
is not counted as a failure. Training images set `GIT_PYTHON_REFRESH=quiet` because Git
metadata is supplied by the platform; the cloudpickle serialization warning remains visible.
Raw copy/download preserves the original output. Kubernetes HTTP bytes are decoded as UTF-8
before serving snapshots, so Python byte repr and escaped newlines never become the log UI.

Recommended producer format is one JSON object per line with `timestamp` (RFC3339), `level`
(`debug`, `info`, `warning`, `error`, `fatal`), `service` and `event`. Add metrics, run IDs and
other diagnostic context as extra fields. Plain output stays supported without inventing
a severity or missing timestamp.

The Go service verifies the signature and checks pod UID, workflow label, workflow owner UID
and container before and after opening `pods/log`, preventing a same-name pod replacement
from being exposed. Its separate ServiceAccount has only `get` on pods and pods/log in
project namespaces bound by the reconciler. It has no database, MLflow or Secret access.
The dedicated signing Secret is mounted only by API and log pods; it must contain at least
32 random bytes. Use a separate Secret from database or OIDC credentials. Restart both
API and log deployments when rotating that key.

Each replica bounds concurrency to 64 streams, eight streams per namespace, a four-line
upstream queue, 64 KiB per line and a 10-second client write deadline. Kubernetes TLS uses
the mounted cluster CA and re-reads the ServiceAccount token for every request. Cancellation
closes upstream logs. Tickets are single-admission within a replica; replay accounting and
limits are local to each replica, not a distributed rate limit. Role revocation takes effect
on the next ticket; an already admitted stream can live for up to five minutes.

Enable explicitly with immutable images:

```yaml
logStreaming:
  enabled: true
  existingSecret: mlp-log-stream-signing
  secretKey: signing-key
  publicURL: /log-stream
  image:
    repository: your-registry/mlp-log-stream
    digest: sha256:REPLACE_WITH_VERIFIED_DIGEST
```

The chart adds the same-origin ingress path, a separate Deployment/Service/ServiceAccount,
a project log ClusterRole, optional NetworkPolicy and a disruption budget. Production
requires a digest and at least two replicas. Namespace bindings preserve ownership and
resource-version checks. Health endpoints are `/healthz` and `/readyz`; `/metrics` exposes
active streams and admission/denial counters. The feature is disabled by default.

Build from the repository root with `services/log-stream-go/Dockerfile`.
Run `make log-stream-go-test` for race tests and vet; CI also builds, scans and emits an SBOM.
