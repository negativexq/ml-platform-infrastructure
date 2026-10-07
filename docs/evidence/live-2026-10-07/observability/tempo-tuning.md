# Tempo historical query follow-up — 2026-10-07

After the recorded 512MiB OOM, the source manifests and acceptance lab now use:

- memory request **256Mi**, limit **1Gi**;
- `querier.max_concurrent_queries: 2` (Tempo 2.7.1 default was 20).

The actual Tempo 2.7.1 binary validates the updated config with `-config.verify=true`.
The first bare `-config.verify` invocation was rejected because that flag requires a
value; the corrected invocation passed before the query check.

## Preservation and replay

Before replacing the emptyDir-backed pod, a private **891,915-byte** live filesystem
snapshot was saved with mode 0600 in a private directory. It is not committed because
raw trace storage may contain query text/context. [Preservation record](tempo-tuning-preservation.json)
retains the hash/size/scope. Existing curated trace summaries were already persisted.
This is a best-effort local snapshot, not a tested disaster-recovery guarantee.

Only the completed immutable backend block was restored into the new pod: block
`e518eac5-650a-4dc5-b4d8-a724bffc94b5`, **1,390 traces**, covering 00:15:38–00:48:11 UTC.
The WAL was excluded. A deliberate clean-exit container restart let Tempo discover the
restored backend block while retaining the new pod's emptyDir. The baseline restart
count for testing is therefore **1**, last termination `Completed`, not an OOM.
The repo manifests contain no special restore/init-container machinery.

## Recorded query result

[Query report](tempo-query-check.json): the same old single-gateway load window search
returns **52 historical gateway traces**. **100 trace reads**, with two concurrent client
queries, all returned 200 with spans. Query p50/p95/p99 were approximately **11/23/46 ms**;
these are a short warm-cache local fixture, not production query latency evidence.

Five freshly sampled `GET /projects` requests also appear in Tempo with native server
and SQL SELECT spans, proving that Collector export continues after the change.
Raw trace bodies/SQL attributes were not written into public evidence.

[Memory/restart report](tempo-tuning.json): after the queries, current memory was
**254 MiB**, peak **344 MiB**, limit 1,024 MiB. Same pod UID and restart count before/after:
**zero additional restarts or OOMs during the test**. Source config and live resources
match. Temporary query port-forward processes were stopped; API/gateway/reconciler
remain at their existing settings.

This closes the recorded historical-query replay check for this laptop dataset. Both
memory limit and concurrency changed, so it does not isolate their individual effects
or prove that 512MiB would be sufficient with concurrency 2. Sustained query/load and
larger-block memory capacity remain open. EmptyDir remains nondurable on pod replacement.

Repeat the read-only check with a forwarded Tempo URL and a JSON file of newly sampled
API trace IDs; use a new evidence path:

```sh
.venv/bin/python scripts/controlplane_tempo_query_check.py \
  --tempo-url http://127.0.0.1:13208 \
  --new-traces /tmp/mlp-acceptance-private/tempo-new-api-traces.json \
  --out /tmp/mlp-tempo-query-repeat.json
```

This lab-specific check uses the recorded historical window; it requires that backend
block to remain available. It refuses to overwrite previous evidence. Actual Tempo config
validation, live query checks, Ruff and Python compile checks passed.
