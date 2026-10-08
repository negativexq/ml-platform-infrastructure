import hashlib
import io
from uuid import uuid4

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("pandas")
pytest.importorskip("boto3")
import pandas as pd  # noqa: E402

from batch_inference.monitoring_worker import execute, psi  # noqa: E402
from batch_inference.worker import BatchError  # noqa: E402


class Storage:
    def __init__(self, data):
        self.data = data

    def get_object(self, **kwargs):
        return {"ContentLength": len(self.data), "Body": io.BytesIO(self.data)}


def setup(reference, observed, feedback=None, fmt="CSV", task="REGRESSION"):
    columns = [
        {"name": "id", "dtype": "string", "nullable": False},
        {"name": "income", "dtype": "number", "nullable": True},
        {
            "name": "prediction",
            "dtype": "number" if task == "REGRESSION" else "string",
            "nullable": False,
        },
    ]
    spec = {
        "model_version_id": str(uuid4()),
        "reference_dataset_id": str(uuid4()),
        "observed_dataset_id": str(uuid4()),
        "feedback_dataset_id": str(uuid4()) if feedback else None,
        "features": ["income"],
        "batch_size": 2,
        "max_rows": 100,
        "max_bytes": 1000000,
        "max_join_bytes": 1000000,
        "minimum_rows": 3,
        "psi_threshold": 0.2,
        "missing_rate_threshold": 0.1,
        "task": task,
        "entity_key": "id",
        "prediction_column": "prediction",
        "label_column": "actual",
    }
    clients = {}
    for role, values in [("REFERENCE", reference), ("OBSERVED", observed), ("FEEDBACK", feedback)]:
        if values is None:
            continue
        frame = pd.DataFrame(values)
        if fmt == "CSV":
            data = frame.to_csv(index=False).encode()
        else:
            buf = io.BytesIO()
            frame.to_parquet(buf, index=False)
            data = buf.getvalue()
        schema = (
            columns
            if role != "FEEDBACK"
            else [columns[0], {"name": "actual", "dtype": columns[2]["dtype"], "nullable": False}]
        )
        spec[role.lower()] = {
            "uri": "s3://datasets/input",
            "format": fmt,
            "columns": schema,
            "row_count": len(frame),
            "checksum_sha256": hashlib.sha256(data).hexdigest(),
        }
        clients[role] = Storage(data)
    return spec, clients


def rows(income=None, predictions=None):
    return {
        "id": ["001", "002", "003", "004"],
        "income": income or [1.0, 2.0, 3.0, 4.0],
        "prediction": predictions or [1.0, 2.0, 3.0, 4.0],
    }


@pytest.mark.parametrize("fmt", ["CSV", "PARQUET"])
def test_stable_and_shifted_distributions_and_missing_values(fmt):
    spec, clients = setup(rows(), rows(), fmt=fmt)
    report = execute(spec, clients, "run/main")
    assert report["status"] == "STABLE" and report["features"][0]["psi"] == 0
    assert report["reference_rows"] == report["observed_rows"] == 4
    spec, clients = setup(rows(), rows([None, None, 100.0, 101.0]), fmt=fmt)
    report = execute(spec, clients, "run/main")
    assert report["status"] == "DRIFTED"
    assert report["features"][0]["missing_rate_change"] == 0.5


def test_low_sample_count_never_claims_stability():
    spec, clients = setup(rows(), rows())
    spec["minimum_rows"] = 5
    report = execute(spec, clients, "run/main")
    assert report["status"] == "INSUFFICIENT_DATA"
    assert report["features"][0]["psi"] is None


def test_delayed_regression_feedback_preserves_ids_and_exposes_coverage():
    truth = {"id": ["001", "002", "003", "absent"], "actual": [1.0, 2.0, 3.0, 4.0]}
    spec, clients = setup(rows(), rows(), truth)
    result = execute(spec, clients, "run/main")["performance"]
    assert result["matched_rows"] == 3
    assert result["coverage"] == 0.75
    assert result["unmatched_predictions"] == result["unmatched_truth"] == 1
    assert result["metrics"] == {"mae": 0, "rmse": 0, "r2": 1}


@pytest.mark.parametrize("which", ["truth", "prediction"])
def test_duplicate_keys_fail_instead_of_expanding_matches(which):
    observed = rows()
    truth = {"id": ["001", "002", "003", "004"], "actual": [1.0, 2.0, 3.0, 4.0]}
    if which == "truth":
        truth["id"][1] = "001"
    else:
        observed["id"][1] = "001"
    spec, clients = setup(rows(), observed, truth)
    with pytest.raises(BatchError, match="duplicate"):
        execute(spec, clients, "run/main")


def test_constant_truth_reports_undefined_r2_without_nan():
    spec, clients = setup(
        rows(), rows(), {"id": ["001", "002", "003", "004"], "actual": [2.0, 2.0, 2.0, 2.0]}
    )
    metrics = execute(spec, clients, "run/main")["performance"]["metrics"]
    assert metrics["mae"] == 1 and metrics["r2"] is None


def test_label_classification_accuracy_and_macro_f1():
    spec, clients = setup(
        rows(predictions=["a", "a", "b", "b"]),
        rows(predictions=["a", "b", "b", "b"]),
        {"id": ["001", "002", "003", "004"], "actual": ["a", "a", "b", "b"]},
        task="CLASSIFICATION",
    )
    metrics = execute(spec, clients, "run/main")["performance"]["metrics"]
    assert metrics["accuracy"] == 0.75
    assert metrics["macro_f1"] == pytest.approx((2 / 3 + 4 / 5) / 2)


def test_checksum_validation_precedes_profiling():
    spec, clients = setup(rows(), rows())
    spec["reference"]["checksum_sha256"] = "0" * 64
    with pytest.raises(BatchError, match="checksum"):
        execute(spec, clients, "run/main")


def test_psi_zero_symmetry_and_new_mass():
    assert psi([10, 0], [10, 0]) == 0
    assert psi([10, 0], [0, 10]) > 0
    assert psi([10, 0], [0, 10]) == pytest.approx(psi([0, 10], [10, 0]))


def test_unseen_categories_have_a_bounded_distinct_bucket():
    spec, clients = setup(rows(["a", "a", "b", "b"]), rows(["new", "new", "new", "new"]))
    for role in ["reference", "observed"]:
        next(column for column in spec[role]["columns"] if column["name"] == "income")["dtype"] = (
            "string"
        )
    result = execute(spec, clients, "run/main")
    assert result["status"] == "DRIFTED" and result["features"][0]["psi"] > 0.2


def test_join_disk_and_row_limits_are_enforced():
    truth = {"id": ["001", "002", "003", "004"], "actual": [1.0, 2.0, 3.0, 4.0]}
    spec, clients = setup(rows(), rows(), truth)
    spec["max_join_bytes"] = 1
    with pytest.raises(BatchError, match="disk limit"):
        execute(spec, clients, "run/main")
    spec["max_join_bytes"] = 1000000
    spec["max_rows"] = 2
    with pytest.raises(BatchError, match="row limit"):
        execute(spec, clients, "run/main")
