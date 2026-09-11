# SLOs

Frozen from what was actually measured, not decided in advance and then
searched for evidence to match. Baseline numbers are the M5/M9 healthy-state
readings; the load-test numbers are from the M10 autoscaling drill
(60 VUs, ~2,935 req/s sustained).

## Availability

**Target: 99.5% of `/predict` requests succeed (non-5xx), evaluated over 5
minutes.**

Measured: **100%** in every drill run to date — 645,809/645,809 during the
M10 load test, 0 failures during the M2 rolling-update and M10 node-drain
drills. 99.5% (not 100%) is the target because a single-node-each local
platform (M8) has real, accepted failure modes — an artifact-store outage or a
node drain landing on a stateful pod — that a 100% target would be dishonest
about.

**Alert:** `HighErrorRate` fires above 5% 5xx sustained for 2 minutes — a
coarser trigger than the SLO because a 2-minute blip under 5% shouldn't page
anyone.

## Latency

**Target: `/predict` p95 < 100ms.**

Measured: **4.99ms p95** at rest (M5), **23.5ms p95 / 32.9ms p95** under the
M10 load test's peak (2,935 req/s across up to 6 pods) — an order of magnitude
under target even saturated. The target is set loose relative to the
measurement on purpose: this is a `Ridge` regressor, not a real model, and a
production model's inference cost will dominate over this platform's own
overhead. 100ms is where the *platform* stops being the bottleneck.

**Alert:** `HighLatency` fires above 500ms p95 sustained for 2 minutes — 5x the
SLO target, because a brief GC pause or cold pod shouldn't page on a target
this tight.

## Error budget

At the 99.5% availability target, the monthly budget is **~3.6 hours** of
non-5xx failures. Nothing here tracks burn rate yet — there is no
multi-window burn-rate alert, only the flat `HighErrorRate` threshold above.
That is a real gap, not an oversight: burn-rate alerting needs weeks of real
traffic history to tune sensibly, which a lab that gets torn down between
sessions does not have.

## Alert rules

Defined in [`observability/alert-rules.yaml`](../observability/alert-rules.yaml),
unit-tested with `promtool test rules` against
[`observability/alert-rules.test.yaml`](../observability/alert-rules.test.yaml)
(5/5 pass), and applied as a `PrometheusRule` generated from the same file by
`scripts/render-prometheus-rule.sh` — one rule source, so the tested rules and
the applied rules cannot drift apart.

| Alert | Fires when | Why this threshold |
| --- | --- | --- |
| `HighErrorRate` | 5xx rate > 5% for 2m | 10x the availability SLO's error rate; short `for` because errors should page fast |
| `HighLatency` | `/predict` p95 > 500ms for 2m | 5x the latency SLO; guards against becoming the F5 regression from M5 (measured 956ms p95 under fault) |
| `ModelNotReady` | `model_ready` is 0 across every pod for 1m | the F2/F3 scenario from M5 — no pod can serve a prediction |
| `InferenceUnavailable` | no scrape target reports `up` for 1m | Prometheus itself lost the service, distinct from the app reporting unhealthy |
| `ReplicaUnavailable` | available replicas < desired for 5m | the F6 bad-rollout scenario from M5, generalised |

Every threshold traces to a number this project actually measured — the F5
drill's 956ms fault, the F2/F3 scenarios' pod state, the M2/M10 rolling-update
and rollout evidence — rather than a guessed "seems reasonable" value.
