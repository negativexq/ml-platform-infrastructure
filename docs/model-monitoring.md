# Model Monitoring implementation contract

Implemented core: immutable dataset checks and delayed-feedback reports.
Operational latency/availability monitoring remains separate
from data drift and predictive quality. The first model-quality checks use the existing
classic-model and immutable S3/MinIO CSV/Parquet dataset contracts.

A managed monitoring Job pins the model version, reference dataset, observed dataset,
selected feature names/types and optional delayed ground-truth dataset. Its immutable
snapshot includes connection scopes, integrity identities, limits and thresholds;
credentials are Kubernetes SecretRefs. Worker execution uses the scientific runtime,
while the control plane retains its minimal dependency profile.

Feature drift compares fixed reference histograms to observed values using population
stability index (PSI), with explicit smoothing, and absolute missing-rate change.
Numeric bin boundaries are determined by the reference range; categorical domains
are bounded, with an explicit unseen-category bucket. Reports show sample counts,
thresholds and insufficient-data states. A drift signal does not establish model error.
Thresholds are configurable rather than asserted as universal significance levels.

Delayed feedback matches unique entity/request identifiers in the observed and ground-truth
datasets. Duplicate keys fail rather than multiplying records. Unmatched counts and
coverage remain visible. Regression reports MAE, RMSE and R²; classification reports
accuracy and macro F1 from predicted labels. Undefined metrics remain null. No AUC is
claimed without a score/probability contract. Matching is bounded and uses local disk.

A successful worker emits a bounded result through the existing Argo output parameter.
The reconciler validates its frozen identities and commits an append-only monitoring
report, run/step state and audit in one transaction. Invalid/missing output fails the
run. Drift is a successful measurement with an attention status, not an execution failure.
Jobs retain existing retry, pipeline and schedule behavior. A retry is a new report;
reconciling a terminal run cannot create duplicate reports.

The UI exposes project-scoped checks/history, feature scores, feedback coverage,
metrics and links to model/dataset/run identities. Report reads require viewer;
creation/start requires operator. Reports and snapshots contain no credential values.

Online sample capture and automatic monitoring of new production windows follow the
core report contract. Capture must be opt-in, bounded, version/revision attributed and
fail-open for serving; request payloads must never be copied into telemetry logs.
Automatic retraining and external notification delivery are separate automation features.

## Boundaries

One pinned S3 object per dataset, at most 32 features, 1,000 reference categories
per feature and 20 classification labels. Worker row/object/join limits are explicit.
Numeric profiles use ten equal-width reference bins; missing and unseen values remain
visible. Reference and observed row minima apply independently.

Scheduling a check reuses its pinned datasets. It does not automatically select a new
production window. Online request capture, rolling-window discovery, predictive-error
threshold alerts and automatic retraining remain future work. The attention inbox
currently signals feature drift, not a configured performance SLA.
