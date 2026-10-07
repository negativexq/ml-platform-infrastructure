# Run parameters

Job and pipeline definitions accept an immutable `parameter_schema`. Runs accept a
`parameters` JSON object. The API validates types, required fields, bounds, enum
choices and date/date-time/URI formats before recording intent. Top-level schema
defaults are materialized into the persisted run snapshot. Unknown fields are
rejected; an empty schema preserves existing parameterless definitions.

```json
{
  "parameter_schema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "processing_date": {"type": "string", "format": "date"},
      "batch_size": {"type": "integer", "minimum": 1, "default": 1000}
    },
    "required": ["processing_date"]
  }
}
```

A run request can then use `{"parameters":{"processing_date":"2026-10-08"}}`.
Container code reads `json.loads(os.environ["MLP_PARAMETERS"])`. This environment
variable is platform-owned and overrides a job's environment entry. Commands are
never expanded or interpolated. Pipeline steps receive the subset declared by
their job's schema, with that job's defaults; missing required step values reject
the pipeline run before any workflow is submitted. Job definitions and pipeline
versions remain immutable.

Idempotency compares the resolved snapshot, so omitted defaults and explicit
identical defaults replay the same run. Different values with the same key return
409. Job retry and pipeline “Run again” preserve the original values and definition.
Execution parameters appear separately from MLflow experiment parameters in run
pages. They are ordinary configuration, not a secret transport; use SecretRefs
for credentials. Audit stores parameter fingerprints, not values.

Schedules accept static `parameters` and `parameter_bindings`. A binding maps a
parameter name to `processing_date` (scheduled occurrence in the schedule's local
timezone) or `scheduled_for` (UTC timestamp). Static and bound keys must not overlap.
Values and defaults are resolved and persisted when the occurrence is recorded,
including QUEUED occurrences without a run. Queue delay, schedule edits and
restarts cannot change them. An incompatible LATEST schema records MISSED with a
sanitized reason and advances the cursor.

The schema is a bounded JSON Schema 2020-12 subset: object/array/scalar types,
properties/required/additionalProperties/items, enum/const, numeric and size bounds,
uniqueItems, default/title/description and date/date-time/URI formats. References,
regex patterns and combinators are disabled. Schemas and values are limited to
64 KiB; schema traversal is limited to six levels and JSON nesting to sixteen.
Defaults apply to top-level fields only. URI values must be absolute and must not
contain embedded username/password credentials.

Migration 0023 adds JSONB schema/snapshot columns with empty-object defaults.
Existing runs keep empty parameters and continue to compile. The gateway accepts
0021/0022/0023 during the additive rollout; API/reconciler still require the exact
head of their own image. This adds jsonschema and three small transitive packages
to the control-plane lock; inference/scientific dependencies remain excluded.
