# S3/MinIO data catalog

Connections are project-scoped immutable metadata: S3 endpoint, region, bucket,
prefix and the name of an existing project credential Secret. The API stores no
credential values and does not proxy S3 requests. Rotate credentials through the
existing Secret API; register a new connection name to change endpoint or scope.
Referenced secrets are protected by the same project lock used for catalog writes.

Dataset versions identify a single CSV or Parquet object. Each version records its
connection, `s3://bucket/object` URI, column names/types/nullability, expected SHA-256
or non-null S3 object version ID, optional row count and producer Job/Pipeline run.
A URI alone does not provide immutable data identity. Registrations are metadata
assertions; this API does not inspect object contents or test connectivity. Consumers
must verify the checksum/version and schema before using a dataset. Batch Inference
performs that verification in its worker, preserving the minimal control-plane image.

Supported column types are `string`, `integer`, `number` and `boolean`; 1–512 unique
columns are required. A dataset must remain in its connection's bucket/prefix and
project. Producer runs must belong to the same project. Credentials embedded in an
endpoint/URI, unversioned S3 `null` version IDs and missing integrity identity are rejected.

Routes:

- `POST/GET /projects/{project}/data-connections`
- `POST/GET /projects/{project}/datasets`
- `GET /projects/{project}/datasets/{name}?version=N` (omitting version selects latest)
- `GET /projects/{project}/data-connections/{id}`
- `GET /projects/{project}/dataset-versions/{id}`
- `GET /projects/{project}/datasets/{name}/lineage?version=N`

Viewer may read; operator may register/publish. The existing project authorization
boundary applies to every route. The catalog has no credential-reading endpoint.
Listings are bounded and paginated. Global Connections and Datasets pages select a
project; the project Data tab exposes the same catalog. Typed forms register
connections and dataset versions. Dataset details display schema, object identity,
producer links, paginated version history and the recorded lineage graph. Secret
usage includes connection references; secret values never appear in the catalog.

Lineage follows committed managed batch output audits to the immutable input
and model version, output-producing job/pipeline run and model-training pipeline.
Manually registered datasets only display explicitly recorded producers. No
upstream relationship is inferred from names or URIs. Graph expansion is bounded
to four dataset hops; open an upstream dataset to continue. Every related resource
is restricted to the current project. This is recorded execution lineage, not a
column-level provenance or an S3 content inspection service.

Publishing a dataset uses `expected_latest_version` (0 for the first version).
A project row lock coordinates publishers, while PostgreSQL uniqueness enforces
`(project_id, name, version)`. Identical latest metadata replays its existing identity;
different metadata with a stale expected version returns 409. Previous versions are
never updated. A composite foreign key additionally enforces connection/project
ownership. Runtime API and reconciler roles may select/insert catalog rows, but may
not update or delete them. Gateway has no catalog privileges. Migration 0024 adds
these tables; backup inventory and explicit role reconfiguration include them.

Example registration (after creating `source-data` and obtaining its connection ID):

```json
{
  "name": "customer-features",
  "expected_latest_version": 0,
  "connection_id": "00000000-0000-0000-0000-000000000000",
  "uri": "s3://datasets/training/customer-features.parquet",
  "format": "PARQUET",
  "columns": [{"name": "income", "dtype": "number", "nullable": false}],
  "object_version_id": "a-real-version-id"
}
```
