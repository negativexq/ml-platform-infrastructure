"""Bounded S3 dataset drift and delayed-feedback checks; no model deserialization."""

from __future__ import annotations

import bisect
import json
import logging
import math
import os
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pandas as pd
import pyarrow.parquet as pq

from batch_inference.worker import (
    RESULT_PATH,
    BatchError,
    client,
    download,
    identity,
    validate_frame,
)

log = logging.getLogger("monitoring")


def frames(path: Path, dataset: dict[str, Any], spec: dict[str, Any]) -> Iterator[pd.DataFrame]:
    if dataset["format"] == "CSV":
        source = pd.read_csv(path, chunksize=spec["batch_size"], dtype="string")
    else:
        parquet = pq.ParquetFile(path)
        if any(
            parquet.metadata.row_group(i).total_byte_size > spec["max_bytes"]
            for i in range(parquet.metadata.num_row_groups)
        ):
            raise BatchError("Parquet row group exceeds uncompressed byte limit")
        source = (
            batch.to_pandas() for batch in parquet.iter_batches(batch_size=spec["batch_size"])
        )
    rows = 0
    for frame in source:
        rows += len(frame)
        if rows > spec["max_rows"]:
            raise BatchError("dataset exceeds row limit")
        yield validate_frame(frame, dataset["columns"])
    if dataset.get("row_count") is not None and rows != dataset["row_count"]:
        raise BatchError("dataset row count differs from registered identity")


def psi(reference: list[int], observed: list[int]) -> float:
    """Jeffreys pseudocount 0.5, normalized independently in each distribution."""
    if len(reference) != len(observed) or not reference:
        raise BatchError("histogram domains differ")
    a_total, b_total = sum(reference) + 0.5 * len(reference), sum(observed) + 0.5 * len(observed)
    return sum(
        ((b + 0.5) / b_total - (a + 0.5) / a_total)
        * math.log(((b + 0.5) / b_total) / ((a + 0.5) / a_total))
        for a, b in zip(reference, observed, strict=True)
    )


def drift(spec: dict[str, Any], paths: dict[str, Path]) -> tuple[list[dict[str, Any]], int, int]:
    columns = {column["name"]: column for column in spec["reference"]["columns"]}
    domains: dict[str, Any] = {}
    for name in spec["features"]:
        domains[name] = (
            [math.inf, -math.inf] if columns[name]["dtype"] in {"number", "integer"} else set()
        )
    # Determine fixed bins from the reference only. A second streaming pass counts them.
    for frame in frames(paths["REFERENCE"], spec["reference"], spec):
        for name, domain in domains.items():
            present = frame[name].dropna()
            if isinstance(domain, list):
                if len(present):
                    domain[0] = min(domain[0], float(present.min()))
                    domain[1] = max(domain[1], float(present.max()))
            else:
                domain.update(str(value) for value in present.unique())
                if len(domain) > 1000:
                    raise BatchError("reference category domain exceeds 1000 values")
                if any(len(value.encode()) > 1024 for value in domain):
                    raise BatchError("category value exceeds byte limit")
    bins: dict[str, Any] = {}
    for name, domain in domains.items():
        if isinstance(domain, list):
            lo, hi = domain
            if lo == math.inf:
                bins[name] = None
            elif hi == lo:
                bins[name] = [lo, math.nextafter(lo, math.inf)]
            else:
                # Weighted interpolation avoids overflowing hi-lo for extreme finite values.
                bins[name] = [lo * (1 - i / 10) + hi * (i / 10) for i in range(1, 10)]
        else:
            bins[name] = {value: index for index, value in enumerate(sorted(domain))}
    counts = {}
    totals = {}
    missing = {}
    for role in ("REFERENCE", "OBSERVED"):
        counts[role] = {
            name: [0] * (len(domain) + 2 if domain is not None else 2)
            for name, domain in bins.items()
        }
        missing[role] = Counter()
        totals[role] = 0
        for frame in frames(paths[role], spec[role.lower()], spec):
            totals[role] += len(frame)
            for name, domain in bins.items():
                missing[role][name] += int(frame[name].isna().sum())
                for value in frame[name]:
                    if pd.isna(value):
                        index = len(counts[role][name]) - 1
                    elif isinstance(domain, list):
                        index = bisect.bisect_right(domain, float(value))
                    elif isinstance(domain, dict):
                        index = domain.get(str(value), len(domain))
                    else:
                        index = 0
                    counts[role][name][index] += 1
    features = []
    enough = min(totals.values()) >= spec["minimum_rows"]
    for name, domain in bins.items():
        a_missing = (
            missing["REFERENCE"][name] / totals["REFERENCE"] if totals["REFERENCE"] else None
        )
        b_missing = missing["OBSERVED"][name] / totals["OBSERVED"] if totals["OBSERVED"] else None
        delta = (
            abs(b_missing - a_missing) if a_missing is not None and b_missing is not None else None
        )
        score = (
            psi(counts["REFERENCE"][name], counts["OBSERVED"][name])
            if enough and domain is not None
            else None
        )
        flagged = enough and (
            (score is not None and score >= spec["psi_threshold"])
            or (delta is not None and delta >= spec["missing_rate_threshold"])
        )
        features.append(
            {
                "name": name,
                "dtype": columns[name]["dtype"],
                "psi": score,
                "reference_missing_rate": a_missing,
                "observed_missing_rate": b_missing,
                "missing_rate_change": delta,
                "drifted": flagged,
                "status": "DRIFTED"
                if flagged
                else "STABLE"
                if enough and score is not None
                else "INSUFFICIENT_DATA",
            }
        )
    return features, totals["REFERENCE"], totals["OBSERVED"]


def key(value: Any) -> str:
    if pd.isna(value):
        raise BatchError("feedback entity key cannot be null")
    encoded = str(value)
    if len(encoded.encode()) > 1024:
        raise BatchError("feedback entity key exceeds byte limit")
    return encoded


def performance(spec: dict[str, Any], paths: dict[str, Path], root: Path) -> dict[str, Any] | None:
    try:
        return _performance(spec, paths, root)
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
            raise BatchError("feedback matching exceeds disk limit") from None
        raise


def _performance(spec: dict[str, Any], paths: dict[str, Path], root: Path) -> dict[str, Any] | None:
    if not spec.get("feedback"):
        return None
    matched = observed = truth_rows = 0
    errors = squared_errors = mean = m2 = 0.0
    labels: set[str] = set()
    confusion: Counter[tuple[str, str]] = Counter()
    database = root / "feedback.sqlite"
    # Reserve the database and its rollback journal separately. Enforce the page
    # ceiling inside SQLite, before a batch can grow the file beyond its budget.
    pages = spec["max_join_bytes"] // 4096
    if pages < 3:
        raise BatchError("feedback matching exceeds disk limit")
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA page_size=4096")
        connection.execute(f"PRAGMA max_page_count={pages}")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("PRAGMA cache_size=-8192")
        connection.execute(
            "CREATE TABLE truth (entity TEXT PRIMARY KEY, value TEXT, matched INTEGER DEFAULT 0)"
        )
        for frame in frames(paths["FEEDBACK"], spec["feedback"], spec):
            for entity, label in zip(
                frame[spec["entity_key"]], frame[spec["label_column"]], strict=True
            ):
                if pd.isna(label):
                    raise BatchError("ground-truth label cannot be null")
                try:
                    connection.execute(
                        "INSERT INTO truth(entity,value) VALUES (?,?)", (key(entity), str(label))
                    )
                except sqlite3.IntegrityError as exc:
                    raise BatchError("duplicate ground-truth entity key") from exc
                truth_rows += 1
            connection.commit()
            if database.stat().st_size > spec["max_join_bytes"]:
                raise BatchError("feedback matching exceeds disk limit")
        connection.execute("CREATE TABLE predictions (entity TEXT PRIMARY KEY)")
        for frame in frames(paths["OBSERVED"], spec["observed"], spec):
            for entity, prediction in zip(
                frame[spec["entity_key"]], frame[spec["prediction_column"]], strict=True
            ):
                entity = key(entity)
                try:
                    connection.execute("INSERT INTO predictions(entity) VALUES (?)", (entity,))
                except sqlite3.IntegrityError as exc:
                    raise BatchError("duplicate prediction entity key") from exc
                observed += 1
                row = connection.execute(
                    "SELECT value FROM truth WHERE entity=?", (entity,)
                ).fetchone()
                if row is None:
                    continue
                if pd.isna(prediction):
                    raise BatchError("matched prediction cannot be null")
                matched += 1
                connection.execute("UPDATE truth SET matched=1 WHERE entity=?", (entity,))
                if spec["task"] == "REGRESSION":
                    actual, predicted = float(row[0]), float(prediction)
                    error = predicted - actual
                    errors += abs(error)
                    squared_errors += error * error
                    delta = actual - mean
                    mean += delta / matched
                    m2 += delta * (actual - mean)
                else:
                    actual, predicted = row[0], str(prediction)
                    labels.update((actual, predicted))
                    if len(labels) > 20:
                        raise BatchError("classification exceeds 20 label domain")
                    confusion[(actual, predicted)] += 1
            connection.commit()
            if database.stat().st_size > spec["max_join_bytes"]:
                raise BatchError("feedback matching exceeds disk limit")
    enough = matched >= spec["minimum_rows"]
    if spec["task"] == "REGRESSION":
        metrics = {
            "mae": errors / matched if enough else None,
            "rmse": math.sqrt(squared_errors / matched) if enough else None,
            "r2": 1 - squared_errors / m2 if enough and m2 > 0 else None,
        }
    else:
        metrics = {"accuracy": None, "macro_f1": None}
        if enough:
            f1 = []
            for label in labels:
                tp = confusion[(label, label)]
                fp = sum(count for (a, p), count in confusion.items() if p == label and a != label)
                fn = sum(count for (a, p), count in confusion.items() if a == label and p != label)
                f1.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0)
            metrics = {
                "accuracy": sum(confusion[(label, label)] for label in labels) / matched,
                "macro_f1": sum(f1) / len(f1),
            }
    if any(value is not None and not math.isfinite(value) for value in metrics.values()):
        raise BatchError("performance calculation overflow")
    return {
        "task": spec["task"],
        "matched_rows": matched,
        "unmatched_predictions": observed - matched,
        "unmatched_truth": truth_rows - matched,
        "coverage": matched / observed if observed else None,
        "status": "MEASURED" if enough else "INSUFFICIENT_DATA",
        "metrics": metrics,
    }


def execute(
    spec: dict[str, Any], clients: dict[str, Any], execution: str | None = None
) -> dict[str, Any]:
    execution = execution or identity()
    paths = {}
    with tempfile.TemporaryDirectory(prefix="mlp-monitoring-") as temporary:
        root = Path(temporary)
        for role in ("REFERENCE", "OBSERVED", "FEEDBACK"):
            dataset = spec.get(role.lower())
            if not dataset:
                continue
            log.info("Verifying %s dataset identity and schema", role.lower())
            uri = urlsplit(dataset["uri"])
            path = root / role.lower()
            _, digest = download(
                clients[role],
                uri.netloc,
                uri.path.lstrip("/"),
                path,
                spec["max_bytes"],
                dataset.get("object_version_id"),
            )
            if dataset.get("checksum_sha256") and digest != dataset["checksum_sha256"]:
                raise BatchError("dataset checksum differs from registered identity")
            paths[role] = path
        log.info("Comparing selected feature distributions")
        features, reference_rows, observed_rows = drift(spec, paths)
        if spec.get("feedback"):
            log.info("Matching delayed ground truth")
        feedback = performance(spec, paths, root)
    status = (
        "DRIFTED"
        if any(feature["drifted"] for feature in features)
        else "STABLE"
        if all(feature["status"] == "STABLE" for feature in features)
        else "INSUFFICIENT_DATA"
    )
    result = {
        "execution": execution,
        "model_version_id": spec["model_version_id"],
        "reference_dataset_id": spec["reference_dataset_id"],
        "observed_dataset_id": spec["observed_dataset_id"],
        "feedback_dataset_id": spec.get("feedback_dataset_id"),
        "status": status,
        "reference_rows": reference_rows,
        "observed_rows": observed_rows,
        "features": features,
        "performance": feedback,
    }
    encoded = json.dumps(result, allow_nan=False)
    if len(encoded.encode()) > 32768:
        raise BatchError("monitoring report exceeds result limit")
    log.info(
        "Monitoring completed: %s; reference=%d observed=%d", status, reference_rows, observed_rows
    )
    return result


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    try:
        raw = os.environ["MLP_MONITORING_SPEC"]
        if len(raw.encode()) > 65536:
            raise BatchError("monitoring specification exceeds limit")
        spec = json.loads(raw)
        clients = {
            role: client(spec[role.lower()], role)
            for role in ("REFERENCE", "OBSERVED", "FEEDBACK")
            if spec.get(role.lower())
        }
        result = execute(spec, clients)
        Path(RESULT_PATH).write_text(json.dumps(result, allow_nan=False))
    except BatchError as error:
        log.error("Monitoring failed: %s", error)
        raise SystemExit(1) from None
    except Exception:
        log.error("Monitoring failed; check registered dataset identity, schema and storage access")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
