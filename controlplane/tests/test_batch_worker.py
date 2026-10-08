import hashlib
import io

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("pandas")
pytest.importorskip("boto3")
import pandas as pd  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from batch_inference.worker import run  # noqa: E402


class Storage:
    def __init__(self, data=b""):
        self.data, self.outputs = data, {}

    def get_object(self, **kwargs):
        self.version = kwargs.get("VersionId")
        return {"ContentLength": len(self.data), "Body": io.BytesIO(self.data)}

    def put_object(self, **kwargs):
        assert kwargs["IfNoneMatch"] == "*"
        if kwargs["Key"] in self.outputs:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        self.outputs[kwargs["Key"]] = (kwargs["Body"].read(), kwargs["Metadata"])

    def head_object(self, **kwargs):
        data, metadata = self.outputs[kwargs["Key"]]
        return {"ContentLength": len(data), "Metadata": metadata}


class Predictor:
    def predict(self, frame):
        return frame["income"].astype(float).to_numpy() * 2


def setup(monkeypatch, input_format="CSV", output_format="CSV"):
    monkeypatch.setenv("MLP_RUN_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    frame = pd.DataFrame({"income": [1.0, 2.0, 3.0]})
    buf = io.BytesIO()
    if input_format == "CSV":
        data = frame.to_csv(index=False).encode()
    else:
        frame.to_parquet(buf, index=False)
        data = buf.getvalue()
    storage = Storage(data)
    spec = dict(
        name="score",
        input=dict(
            uri="s3://datasets/input",
            format=input_format,
            columns=[dict(name="income", dtype="number", nullable=False)],
            row_count=3,
            checksum_sha256=hashlib.sha256(data).hexdigest(),
        ),
        output=dict(bucket="datasets", prefix="predictions"),
        model_uri="s3://datasets/model",
        output_format=output_format,
        prediction_dtype="number",
        batch_size=2,
        features=["income"],
        max_rows=10,
        max_bytes=1000000,
        max_model_bytes=1000000,
    )
    return spec, storage


@pytest.mark.parametrize("input_format", ["CSV", "PARQUET"])
@pytest.mark.parametrize("output_format", ["CSV", "PARQUET"])
def test_chunked_prediction_and_conditional_replay(monkeypatch, input_format, output_format):
    spec, storage = setup(monkeypatch, input_format, output_format)
    clients = {role: storage for role in ["INPUT", "MODEL", "OUTPUT"]}
    result = run(spec, clients, lambda *args: Predictor())
    assert result["row_count"] == 3
    data, _ = next(iter(storage.outputs.values()))
    frame = (
        pd.read_csv(io.BytesIO(data))
        if output_format == "CSV"
        else pd.read_parquet(io.BytesIO(data))
    )
    assert frame["prediction"].tolist() == [2.0, 4.0, 6.0]
    assert hashlib.sha256(data).hexdigest() == result["checksum_sha256"]
    assert run(spec, clients, lambda *args: Predictor()) == result
    key = next(iter(storage.outputs))
    storage.outputs[key] = (data, {"mlp-execution": "different"})
    with pytest.raises(ValueError, match="conflict"):
        run(spec, clients, lambda *args: Predictor())


def test_checksum_is_verified_before_loading_model(monkeypatch):
    spec, storage = setup(monkeypatch)
    spec["input"]["checksum_sha256"] = "0" * 64

    def forbidden(*args):
        pytest.fail("model must not load on corrupt input")

    with pytest.raises(ValueError, match="checksum"):
        run(spec, {role: storage for role in ["INPUT", "MODEL", "OUTPUT"]}, forbidden)
    assert not storage.outputs


@pytest.mark.parametrize("change", [{"max_rows": 2}, {"max_bytes": 3}, {"features": ["missing"]}])
def test_limits_fail_before_output_publish(monkeypatch, change):
    spec, storage = setup(monkeypatch)
    spec.update(change)
    with pytest.raises((ValueError, KeyError)):
        run(
            spec,
            {role: storage for role in ["INPUT", "MODEL", "OUTPUT"]},
            lambda *args: Predictor(),
        )
    assert not storage.outputs


def test_versioned_input_and_schema_mismatch(monkeypatch):
    spec, storage = setup(monkeypatch)
    spec["input"]["object_version_id"] = "version-1"
    spec["input"]["columns"][0]["name"] = "different"
    with pytest.raises(ValueError, match="columns"):
        run(
            spec,
            {role: storage for role in ["INPUT", "MODEL", "OUTPUT"]},
            lambda *args: Predictor(),
        )
    assert storage.version == "version-1"
