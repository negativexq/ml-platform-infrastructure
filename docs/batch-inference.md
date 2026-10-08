# Batch inference

Managed batch definitions pin a classic model version and one immutable dataset version.
They are stored as immutable job definitions with a structured `batch_spec`; ordinary jobs
cannot set this field through the job creation API. Existing run history, idempotency,
retry/cancel, pipeline DAGs and JOB schedules apply without a parallel execution engine.

Configure `CP_BATCH_IMAGE` with the digest of `docker/batch-inference/Dockerfile`.
Without it, creation fails closed. Credentials come from project-local connection Secrets,
with independent input, model and output references; values never enter platform metadata.

`POST /projects/{project}/batch-inference` selects `input_dataset_id`, `model_version_id`,
`model_connection_id`, `output_connection_id`, `output_dataset`, ordered `features`,
`output_format` (CSV/PARQUET), `prediction_dtype` and bounded runtime limits.
List/get definitions at the same resource. Start via `/projects/{project}/jobs/{name}/runs`;
use its job name in pipelines or schedules. The Batch Inference page provides creation and
Run now with configurable CPU/memory, with existing job details exposing history and scheduling.

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
publication prevents overwrite. A same-execution replay accepts an existing object only if
its execution metadata, content hash, row count and size match. Prediction or input-validation failure publishes no output object. A failure after
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

Schedules reuse this pinned input version; registering newer data does not silently change
an existing batch definition. Create a new definition for a new dataset version. Dynamic input
selection, partition fan-out, streaming sources, moving latest model aliases and multi-output
models are outside this initial single-object contract. Model version and resolved artifact
URI are frozen; integrity of externally mutable model artifacts remains the artifact owner's
responsibility, as with the existing serving contract.

Official runtime contracts: [conditional S3 PUT](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/put_object.html),
[Parquet record batches](https://arrow.apache.org/docs/python/generated/pyarrow.parquet.ParquetFile.html),
[MLflow pyfunc loading](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.pyfunc.html).
