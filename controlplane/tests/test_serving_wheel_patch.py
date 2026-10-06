"""The compatibility fork must preserve code and reject unverified source wheels."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import zipfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "serving_patch", Path(__file__).resolve().parents[2] / "docker/serving/patch_mlserver.py"
)
assert SPEC and SPEC.loader
patch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patch)


def source_wheel(metadata: str, *, signed: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as wheel:
        wheel.writestr("mlserver/server.py", b"unchanged inference implementation")
        wheel.writestr("mlserver-1.7.1.dist-info/METADATA", metadata)
        wheel.writestr("mlserver-1.7.1.dist-info/RECORD", "")
        if signed:
            wheel.writestr("mlserver-1.7.1.dist-info/RECORD.jws", "signature")
    return buffer.getvalue()


METADATA = "Version: 1.7.1\nRequires-Dist: fastapi (>=0.88.0,!=0.89.0,<0.116.0)\n"


def test_unverified_source_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="checksum"):
        patch.build(source_wheel(METADATA), tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("signed,metadata", [(True, METADATA), (False, "Version: 9.0\n")])
def test_signed_or_unexpected_metadata_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signed: bool, metadata: str
) -> None:
    source = source_wheel(metadata, signed=signed)
    monkeypatch.setattr(patch, "SOURCE_SHA256", hashlib.sha256(source).hexdigest())
    with pytest.raises(ValueError):
        patch.build(source, tmp_path)
    assert not list(tmp_path.iterdir())


def test_fork_is_reproducible_and_preserves_code_and_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = source_wheel(METADATA)
    monkeypatch.setattr(patch, "SOURCE_SHA256", hashlib.sha256(source).hexdigest())
    first = patch.build(source, tmp_path / "first")
    second = patch.build(source, tmp_path / "second")
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as wheel:
        assert wheel.read("mlserver/server.py") == b"unchanged inference implementation"
        metadata = wheel.read("mlserver-1.7.1+mlp.1.dist-info/METADATA").decode()
        assert "Version: 1.7.1+mlp.1" in metadata
        assert "fastapi (>=0.142.2,<1)" in metadata
        assert "starlette (>=1.3.1,<2)" in metadata
        for name, digest, size in csv.reader(
            io.StringIO(wheel.read("mlserver-1.7.1+mlp.1.dist-info/RECORD").decode())
        ):
            if not digest:
                assert name.endswith("/RECORD")
                continue
            content = wheel.read(name)
            expected = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
            assert digest == "sha256=" + expected.decode()
            assert int(size) == len(content)
