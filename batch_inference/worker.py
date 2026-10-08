"""Bounded single-object classic ML inference; no control-plane credentials or callbacks."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import boto3
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.config import Config
from botocore.exceptions import ClientError

log = logging.getLogger("batch")
RESULT_PATH = "/tmp/mlp-result.json"


class BatchError(ValueError):
    """Safe operator message, containing no input values or credentials."""


def identity() -> str:
    from uuid import UUID

    run = os.environ.get("MLP_RUN_ID") or os.environ["MLP_PIPELINE_RUN_ID"]
    UUID(run)
    step = os.environ.get("MLP_STEP", "main")
    if not step or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-" for c in step):
        raise BatchError("invalid step identity")
    return f"{run}/{step}"


def output_key(spec: dict[str, Any], execution: str) -> str:
    suffix = "csv" if spec["output_format"] == "CSV" else "parquet"
    return f"{spec['output']['prefix'].rstrip('/')}/{spec['name']}/{execution}.{suffix}".lstrip("/")


def client(connection: dict[str, Any], role: str) -> Any:
    prefix = f"BATCH_{role}_"
    return boto3.client(
        "s3",
        endpoint_url=connection["endpoint"],
        region_name=connection["region"],
        aws_access_key_id=os.environ[prefix + "ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ[prefix + "SECRET_ACCESS_KEY"],
        aws_session_token=os.environ.get(prefix + "SESSION_TOKEN"),
        config=Config(
            connect_timeout=10,
            read_timeout=60,
            retries={"max_attempts": 3},
            s3={"addressing_style": "path"},
        ),
    )


def download(
    s3: Any, bucket: str, key: str, target: Path, limit: int, version: str | None = None
) -> tuple[int, str]:
    response = s3.get_object(Bucket=bucket, Key=key, **({"VersionId": version} if version else {}))
    body = response["Body"]
    size, digest = 0, hashlib.sha256()
    try:
        if response["ContentLength"] > limit:
            raise BatchError("object exceeds byte limit")
        with target.open("wb") as dest:
            while chunk := body.read(min(1024 * 1024, limit - size + 1)):
                size += len(chunk)
                if size > limit:
                    raise BatchError("object exceeds byte limit")
                digest.update(chunk)
                dest.write(chunk)
        if size != response["ContentLength"]:
            raise BatchError("incomplete object download")
    finally:
        body.close()
    return size, digest.hexdigest()


def load_model(s3: Any, uri: str, root: Path, limit: int) -> Any:
    import mlflow.pyfunc

    parsed = urlsplit(uri)
    prefix = parsed.path.lstrip("/").rstrip("/") + "/"
    total, count = 0, 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=parsed.netloc, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.endswith("/"):
                continue
            relative = key[len(prefix) :]
            path = PurePosixPath(relative)
            if not relative or path.is_absolute() or ".." in path.parts or "\\" in relative:
                raise BatchError("unsafe model object path")
            count += 1
            if count > 1000 or total + obj["Size"] > limit:
                raise BatchError("model exceeds artifact limits")
            target = root.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            size, _ = download(s3, parsed.netloc, key, target, limit - total)
            total += size
    if not (root / "MLmodel").is_file():
        raise BatchError("MLflow model metadata missing")
    # Never install dependencies or execute environment managers from model metadata.
    return mlflow.pyfunc.load_model(str(root))


def arrow_schema(columns: list[dict[str, Any]]) -> pa.Schema:
    types = {
        "string": pa.string(),
        "integer": pa.int64(),
        "number": pa.float64(),
        "boolean": pa.bool_(),
    }
    return pa.schema(
        [pa.field(c["name"], types[c["dtype"]], nullable=c["nullable"]) for c in columns]
    )


def validate_frame(frame: pd.DataFrame, columns: list[dict[str, Any]]) -> pd.DataFrame:
    if list(frame.columns) != [c["name"] for c in columns]:
        raise BatchError("input columns differ from pinned dataset schema")
    converted = frame.copy()
    for c in columns:
        values = frame[c["name"]]
        if not c["nullable"] and values.isna().any():
            raise BatchError("null in nonnullable input column")
        kind = c["dtype"]
        if kind in {"number", "integer"}:
            values = pd.to_numeric(values, errors="raise")
            present = values.dropna()
            if not np.isfinite(present.to_numpy(dtype=float)).all():
                raise BatchError("nonfinite input number")
            if kind == "integer" and ((present % 1) != 0).any():
                raise BatchError("fractional integer input")
            values = values.astype("Int64" if kind == "integer" else "Float64")
        elif kind == "boolean":
            mapped = values.map(
                lambda v: (
                    v
                    if isinstance(v, (bool, np.bool_)) or pd.isna(v)
                    else {"true": True, "false": False}.get(str(v).lower(), "invalid")
                )
            )
            if (mapped.dropna() == "invalid").any():
                raise BatchError("invalid boolean input")
            values = mapped.astype("boolean")
        else:
            values = values.astype("string")
        converted[c["name"]] = values
    return converted


def predict_file(
    spec: dict[str, Any], source: Path, target: Path, model: Any
) -> tuple[int, list[dict[str, Any]]]:
    columns = spec["input"]["columns"]
    output_columns = columns + [
        {"name": "prediction", "dtype": spec["prediction_dtype"], "nullable": False}
    ]
    schema = arrow_schema(output_columns)
    rows, writer = 0, None
    last_report = 0.0
    if spec["input"]["format"] == "CSV":
        # Strings prevent chunk-dependent inference and retain lexical identifiers.
        frames = pd.read_csv(source, chunksize=spec["batch_size"], dtype="string")
    else:
        parquet = pq.ParquetFile(
            source, thrift_string_size_limit=1024 * 1024, thrift_container_size_limit=100000
        )
        if (
            sum(
                parquet.metadata.row_group(i).total_byte_size
                for i in range(parquet.metadata.num_row_groups)
            )
            > spec["max_bytes"]
        ):
            raise BatchError("Parquet uncompressed data exceeds byte limit")
        if list(parquet.schema_arrow.names) != [c["name"] for c in columns]:
            raise BatchError("Parquet schema columns differ")
        frames = (
            batch.to_pandas() for batch in parquet.iter_batches(batch_size=spec["batch_size"])
        )
    try:
        for frame in frames:
            frame = validate_frame(frame, columns)
            rows += len(frame)
            if rows > spec["max_rows"]:
                raise BatchError("input exceeds row limit")
            values = np.asarray(model.predict(frame[spec["features"]]))
            if values.ndim == 2 and values.shape[1] == 1:
                values = values[:, 0]
            if values.ndim != 1 or len(values) != len(frame):
                raise BatchError("model must return one scalar prediction per input row")
            frame["prediction"] = values
            frame = validate_frame(frame, output_columns)
            table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False, safe=True)
            if spec["output_format"] == "CSV":
                frame.to_csv(target, index=False, mode="a", header=writer is None)
                writer = True
            else:
                if writer is None:
                    writer = pq.ParquetWriter(target, schema, compression="snappy")
                writer.write_table(table)
            if target.stat().st_size > spec["max_bytes"]:
                raise BatchError("output exceeds byte limit")
            if time.monotonic() - last_report >= 10:
                log.info("prediction progress rows=%d", rows)
                last_report = time.monotonic()
        if writer is None:
            if spec["output_format"] == "CSV":
                pd.DataFrame(columns=schema.names).to_csv(target, index=False)
            else:
                pq.write_table(pa.Table.from_batches([], schema=schema), target)
    finally:
        if isinstance(writer, pq.ParquetWriter):
            writer.close()
    if target.stat().st_size > spec["max_bytes"]:
        raise BatchError("output exceeds byte limit")
    expected = spec["input"].get("row_count")
    if expected is not None and rows != expected:
        raise BatchError("input row count differs from pinned dataset")
    return rows, output_columns


def run(
    spec: dict[str, Any], clients: dict[str, Any], model_loader: Any = load_model
) -> dict[str, Any]:
    execution = identity()
    with tempfile.TemporaryDirectory(prefix="mlp-batch-") as temp:
        root = Path(temp)
        source, output = root / "input", root / "output"
        input_spec = spec["input"]
        uri = urlsplit(input_spec["uri"])
        log.info("validating pinned input dataset")
        _, checksum = download(
            clients["INPUT"],
            uri.netloc,
            uri.path[1:],
            source,
            spec["max_bytes"],
            input_spec.get("object_version_id"),
        )
        if input_spec.get("checksum_sha256") and checksum != input_spec["checksum_sha256"]:
            raise BatchError("input checksum differs from pinned dataset")
        log.info("loading pinned model")
        model = model_loader(
            clients["MODEL"], spec["model_uri"], root / "model", spec["max_model_bytes"]
        )
        log.info("starting prediction")
        rows, columns = predict_file(spec, source, output, model)
        digest = hashlib.sha256()
        with output.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        checksum = digest.hexdigest()
        key, bucket = output_key(spec, execution), spec["output"]["bucket"]
        metadata = {"mlp-execution": execution, "sha256": checksum, "rows": str(rows)}
        log.info("publishing verified output rows=%d", rows)
        with output.open("rb") as stream:
            try:
                clients["OUTPUT"].put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=stream,
                    ContentLength=output.stat().st_size,
                    IfNoneMatch="*",
                    Metadata=metadata,
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in {"PreconditionFailed", "412"}:
                    raise
                head = clients["OUTPUT"].head_object(Bucket=bucket, Key=key)
                if (
                    head.get("Metadata") != metadata
                    or head["ContentLength"] != output.stat().st_size
                ):
                    raise BatchError("output object conflict") from exc
        return {
            "execution": execution,
            "uri": f"s3://{bucket}/{key}",
            "checksum_sha256": checksum,
            "row_count": rows,
            "columns": columns,
            "format": spec["output_format"],
        }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        raw = os.environ["MLP_BATCH_SPEC"]
        if len(raw.encode()) > 65536:
            raise BatchError("oversized batch specification")
        spec = json.loads(raw)
        result = run(
            spec, {role: client(spec[role.lower()], role) for role in ("INPUT", "MODEL", "OUTPUT")}
        )
        Path(RESULT_PATH).write_text(json.dumps(result, allow_nan=False), encoding="utf-8")
        log.info("completed rows=%d output=%s", result["row_count"], result["uri"])
    except BatchError as exc:
        log.error("%s", exc)
        raise SystemExit(1) from None
    except Exception:
        # Credentials, row values and model exception messages must never reach logs.
        log.error(
            "batch failed; check dataset integrity/schema, model compatibility and storage access"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
