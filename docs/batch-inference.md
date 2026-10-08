# Batch inference

Managed batch definitions pin a classic model version and select input using `PINNED`,
`LATEST_AT_EXECUTION` or `BY_PROCESSING_DATE`. Every run freezes the actual dataset version.
They are stored as immutable job definitions with a structured `batch_spec`; ordinary jobs
cannot set this field through the job creation API. Existing run history, idempotency,
retry/cancel, pipeline DAGs and JOB schedules apply without a parallel execution engine.

Configure `CP_BATCH_IMAGE` with the digest of `docker/batch-inference/Dockerfile`.
Without it, creation fails closed. Credentials come from project-local connection Secrets,
with independent input, model and output references; values never enter platform metadata.

`POST /projects/{project}/batch-inference` selects `input_dataset_id`, `model_version_id`,
`model_connection_id`, `model_manifest`, `input_selection_policy`, `output_connection_id`,
`output_dataset`, ordered `features`,
`output_format` (CSV/PARQUET), `prediction_dtype` and bounded runtime limits.
List/get definitions at the same resource. Start via `/projects/{project}/jobs/{name}/runs`;
use its job name in pipelines or schedules. The Batch Inference page provides creation and
Run now with configurable CPU/memory and automatic disk reservations, with existing job details exposing history and scheduling.

The worker downloads and verifies input SHA-256 and/or an explicit S3 VersionId before
loading the model. CSV is chunked; Parquet uses record batches. It validates ordered columns,
logical types, nullability, finite numbers and the declared row count. Each prediction batch
uses only the selected features. Output preserves input columns and appends `prediction`.
Progress is reported at most once per ten seconds; validation and publication have concise stage messages.
Only one scalar prediction per row is supported; multi-output models fail explicitly.

Input/output are single S3 objects, not wildcard datasets. Defaults: 1,000 rows per batch,
10 million rows, 1 GiB input/output and 512 MiB model artifacts; configurable ceilings are
100,000 rows per batch, 100 million rows, 2 GiB input/output and 1 GiB model artifacts.
Parquet uncompressed row-group sizes are bounded as well. Model prefixes have a 1,000-file
limit, byte bounds and path traversal checks. Models must be platform-trusted MLflow pyfunc
artifacts compatible with the image; the worker does not install model dependencies.
These are resource ceilings, not throughput claims. Container memory limits remain essential.

Output keys contain batch name, platform run UUID and step name. Conditional `IfNoneMatch=*`
publication prevents overwrite. A same-execution replay first compares HEAD execution metadata,
row count and size, then streams the existing object and verifies its actual SHA-256 against the
newly generated output. GET uses `IfMatch` when HEAD provides an ETag to guard changes between
the requests; the ETag itself is not treated as a content checksum. Reads are bounded by the
expected output size plus one byte, and the response is always closed. Same-size replacements
with copied metadata fail. Verification needs no additional disk copy, but replay requires
S3 GET permission and transfers the output bytes again. Prediction or input-validation failure
publishes no output object. A failure after
upload may leave an orphan as described below. A platform retry creates a new run UUID and a separate object.

Argo captures a bounded JSON result parameter. The reconciler verifies its identity, URI,
format, schema and row bounds against the immutable definition, then commits the output
DatasetVersion, successful run/step CAS and lineage audit in one database transaction.
Missing or invalid results fail the platform run. In a pipeline, invalid batch output fails
the step and pipeline even if Argo reports workflow success. Other DAG steps may already
have run: no distributed rollback or exactly-once side-effect claim is made.

A crash after S3 publication but before database commit leaves an unregistered output object;
if Argo retains a successful result, the next reconcile can recover it. A worker crash before
emitting its result fails that execution; a platform retry is a new run. Transaction rollback
cannot leave a successful platform run without its catalog entry. Cleanup of orphaned objects is a storage retention
policy. Dataset output lists can filter `producer_run_id` or `producer_pipeline_run_id`;
run pages show output versions, rows, location and checksum. The immutable batch definition
and `batch.output_published` audit connect the input dataset and model version to each output.

Migration `0029` constrains both dataset producer links with `(producer_id, project_id)`
foreign keys. Unattributed imported datasets remain valid; cross-project Job/Pipeline
producers fail even for direct SQL writes. The migration validates existing links and
rolls back on inconsistent data, requiring explicit remediation instead of rewriting lineage.

Replay verification establishes the fetched object's integrity at that read. Storage actors
with overwrite permission can still change it later; downstream checksum checks detect such
changes on consumption. Storage-level immutability requires version-pinned reads or an
appropriate write-once/access policy. The platform does not enable these bucket policies.

## Disk resource contract

Managed workers set explicit `ephemeral-storage` requests and limits. Batch reserves
`2 * max_bytes + max_model_bytes`, plus the greater of 512 MiB or 10% scratch space,
rounded up to MiB. Defaults reserve 3 GiB; maximum byte limits reserve 5.5 GiB.
Output CSV and Parquet writes, including footer writes, are checked before reaching disk.
A custom disk reservation must meet the computed minimum. The compiler also derives this
reservation for older definitions that omitted disk resources, without changing stored definitions.

Creation rejects reservations that cannot fit the default project request/limit quotas,
including the Argo executor. Before creating a workflow, the reconciler reads the namespace's
actual LimitRange/ResourceQuota and node allocatable storage to reject impossible pods.
An already submitted workflow is adopted before capacity checks, preserving crash replay.
The check considers total capacity rather than reserving current free space: Kubernetes pod
admission and scheduling remain authoritative for concurrent requests, and node disk pressure
can still evict a workload. Logs, writable layers and worker temporary files use local storage;
this does not use RAM-backed temporary volumes.

## Immutable model package

`model_manifest` is a complete list of relative file paths, byte sizes and lowercase SHA-256
hashes, with optional immutable S3 `object_version_id` per file. Generate it from the trusted
local MLflow package before publishing:

```sh
python -m controlplane.domain.model_artifacts ./model > model-manifest.json
```

Pass the JSON array in the batch creation request or paste it in the creation form. It must
include `MLmodel`. Duplicate paths, traversal, file/directory conflicts and oversize packages
are rejected. File ordering is canonicalized in the immutable definition. The worker fetches
only these objects, verifies every size/hash before MLflow deserialization, and loads only
that verified local directory. Extra objects in the prefix do not enter the model package.
Changing or deleting a referenced object fails the execution instead of silently changing
predictions. Hashes establish content identity, not the authenticity of a supplied package;
models and their manifests must originate from trusted producers.

Old definitions without manifests cannot start new workflows after this release. Publish a
manifest and create a new batch definition name. Active workflows are not cancelled. This
contract covers the local model package; custom model code must not load mutable remote
artifacts at prediction time. It does not change the existing online serving contract.

## Dataset selection and execution snapshots

`PINNED` remains the default and uses `input_dataset_id` directly. `LATEST_AT_EXECUTION`
resolves the dataset name's newest catalog version when the platform run is created.
`BY_PROCESSING_DATE` resolves the newest version whose catalog `processing_date` matches
that run's required `processing_date` parameter. There is no fallback to another day or a
mutable latest URI. Selected inputs must retain the definition's connection, format and
ordered column schema; incompatible latest versions fail explicitly.

For date-based schedules set `parameter_bindings: {"processing_date": "processing_date"}`.
The existing schedule binding uses the planned occurrence's local date in its timezone;
queued executions keep that date even if dispatch occurs on a later day. A queued execution
has no run yet; latest input is resolved when it is dispatched. Missing or incompatible data
records a `MISSED` execution with no run, allowing other schedules to proceed.

Run creation freezes the full selected dataset metadata in `runs.batch_snapshot` or each
pipeline step's `pipeline_runs.batch_snapshots`, atomically with run/step creation and audit.
Project locking coordinates resolution with catalog publication. Workers and result validation
use this snapshot; later dataset versions and idempotency replays cannot change it. Job retry
copies the original snapshot. A new execution resolves again. Run pages show the actual input
version, date and selection policy; output dataset versions inherit the processing date, and
the publication audit records the resolved dataset ID.

Partition fan-out, streaming sources, moving latest model aliases and multi-output models
remain outside the single-object contract.

Official runtime contracts: [conditional S3 PUT](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html),
[conditional S3 GET](https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html),
[PostgreSQL composite foreign keys](https://www.postgresql.org/docs/current/ddl-constraints.html),
[Parquet record batches](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetFile.html),
[MLflow pyfunc loading](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.pyfunc.html).
