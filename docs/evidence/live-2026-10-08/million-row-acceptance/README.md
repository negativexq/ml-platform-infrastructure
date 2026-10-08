# One million NYC taxi trips — local end-to-end acceptance

Actual public [NYC TLC Yellow Taxi trip records](https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page)
for January and February 2025 were downloaded from the linked official Parquet files.
The target is the recorded `fare_amount` in USD, not a fabricated label or the total
including tips/fees. TLC's source page describes the provider origin and data limitations.
Source hashes and preparation steps are retained with the external evidence.

## Data and model

The January source has **3,475,226** rows, February **3,577,543**. Cleaning retains rows
whose pickup falls within the stated month, distance is 0.1–50 miles, passenger count
1–6 and fare USD 2–200. Eligible counts are **2,808,158 / 2,652,940**. Exactly **one million
rows per month** are sampled without replacement with seed `20261008`, preserving source
row identity as `month:source_row`. Parquet row groups contain 10,000 rows.

Model features are distance, passenger count, pickup hour and weekday. A local bootstrap
HistGradientBoostingRegressor (`max_iter=60`, `max_leaf_nodes=31`, seed `20261008`) trains
on 200,000 January rows; 800,000 other January rows are held out. Its trusted MLflow
package is registered and its SHA-256 manifest frozen in the managed batch definition.
This is an illustrative fare model, not a production model or a platform training run.
January held-out MAE/RMSE/R² are **2.091 / 4.568 / 0.921**.

## Actual platform execution

The retained project is **`nyc-taxi-million-3f3f9b`**. Its catalog contains:

| Dataset | Version | Meaning | Rows |
|---|---|---|---:|
| `taxi-features` | v1 | January reference features | 1,000,000 |
| `taxi-features` | v2 | February scoring features | 1,000,000 |
| `taxi-ground-truth` | v1 | January recorded fares | 1,000,000 |
| `taxi-ground-truth` | v2 | February recorded fares | 1,000,000 |
| `fare-predictions` | v1 | Scheduled pipeline's February predictions | 1,000,000 |
| `fare-predictions` | v2 | Independent rerun, separate output object | 1,000,000 |

`scheduled-february-scoring` actually dispatches `monthly-fare-scoring` v2 through the
platform scheduler and Argo. BY_PROCESSING_DATE resolves `2025-02-01` to `taxi-features`
v2 and freezes that identity. This is a historical-window replay triggered now; the
business date does not claim that the observations were collected today. The schedule
is paused after dispatch to avoid repeated background work.

The successful scheduled pipeline is **`48248b44-c738-4e55-aba7-3576e23b5b3a`**. It publishes
`fare-predictions` v1 with producer/step lineage and a verified checksum. A second pipeline
publishes v2 to a different S3 object with **identical prediction bytes**. Replaying that
creation's idempotency key returns the same run. The frozen input identity remains the same.

The managed `february-drift-and-quality` Job then pins the produced v1 dataset and matches
it to the separate February ground truth via unique `trip_id`. All **1,000,000** rows match;
coverage is **100%**, with **0 unmatched predictions and 0 unmatched labels**. An independent
pandas join/sklearn metric calculation agrees with the platform report within floating-point
roundoff. The report is **`9f52a1f9-d624-4176-8f56-dd838103e13c`**:

| Measurement | Result |
|---|---:|
| MAE | 2.082363 USD |
| RMSE | 4.466033 USD |
| R² | 0.920104 |
| Distance PSI | 0.000405 |
| Passenger count PSI | 0.001404 |
| Pickup hour PSI | 0.000835 |
| Weekday PSI | 0.007974 |

All selected feature distributions report **STABLE** under configured PSI threshold 0.2
and missing-rate threshold 0.1. This describes these cleaned, sampled windows and selected
features; it does not imply absence of all model/data problems. No drift was injected.
Monitoring succeeds independently of whether drift is detected.

## Resources, boundaries and retained UI

Platform-reported execution durations are **18.95 s scoring / 28.17 s monitoring**, including
Argo lifecycle overhead. Both main containers explicitly request/limit **1 CPU / 1 GiB RAM**.
Disk reservations are **3 GiB / 5.5 GiB**. Seven live samples read cgroup memory and `/tmp`
usage at a nominal three-second interval: highest observed memory peaks are **262.52 MiB /
276.96 MiB**, and highest sampled `/tmp` use **16.98 MiB / 119.44 MiB**. Sampling does not
establish final absolute disk/memory maxima. Cgroup peak observations are for the main
container, not the complete pod or node.

An initial 2-GiB-memory definition waited because the local node already reserved
8,128 MiB of its approximately 9.7 GiB allocatable memory. That pending run was cancelled;
a new immutable 1-GiB definition/pipeline version succeeded. No node capacity was added
and no namespace/storage contract was bypassed. These single-node, warm-image runs are
not production throughput/capacity guarantees.

Monitoring is submitted after the catalog publishes the scoring result; it is **not**
a dynamically bound downstream step in the same DAG. Automatic new-output binding and
rolling monitoring-window selection remain product work. Ground-truth here is separated
from inputs before scoring, not collected later from a live production system.

The real datasets, model, paused schedule, output versions and report remain in MinIO,
PostgreSQL and the local UI for review:

- [Datasets](http://127.0.0.1:18905/ui/#/projects/nyc-taxi-million-3f3f9b/datasets)
- [Pipeline run](http://127.0.0.1:18905/ui/#/projects/nyc-taxi-million-3f3f9b/pipeline-runs/48248b44-c738-4e55-aba7-3576e23b5b3a)
- [Drift/performance report](http://127.0.0.1:18905/ui/#/projects/nyc-taxi-million-3f3f9b/model-monitoring/reports/9f52a1f9-d624-4176-8f56-dd838103e13c)

Raw source/derived data, model package, result JSON, helpers and logs are outside Git at
`/Users/ofk/ml-platform-infra-artifacts/2026-10-08/million-row-acceptance/`.
[ARTIFACTS.sha256](ARTIFACTS.sha256) records their identities. Only this compact summary
and hashes are committed. Feature Store remains outside this work.
