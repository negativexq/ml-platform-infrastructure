import hashlib

import pytest

from controlplane.domain.model_artifacts import directory_manifest, validate_manifest


def entry(path="MLmodel", **extra):
    return {"path": path, "size": 1, "sha256": "a" * 64, **extra}


@pytest.mark.parametrize(
    "manifest",
    [
        None,
        [],
        [entry("../MLmodel")],
        [entry("/MLmodel")],
        [entry("./MLmodel")],
        [entry("a//MLmodel")],
        [entry("MLmodel\\file")],
        [entry(), entry()],
        [entry(size=True)],
        [entry(sha256="not-sha")],
        [entry(object_version_id="null")],
        [entry("weights")],
        [entry(), entry("MLmodel/weights")],
    ],
)
def test_manifest_rejects_unsafe_incomplete_or_ambiguous_packages(manifest):
    with pytest.raises(ValueError):
        validate_manifest(manifest, 100)


def test_manifest_is_sorted_bounded_and_generated_from_package(tmp_path):
    (tmp_path / "MLmodel").write_bytes(b"metadata")
    (tmp_path / "weights").write_bytes(b"weights")
    manifest = directory_manifest(tmp_path)
    assert manifest[0] == {
        "path": "MLmodel",
        "size": 8,
        "sha256": hashlib.sha256(b"metadata").hexdigest(),
    }
    assert validate_manifest(list(reversed(manifest)), 100) == manifest
    with pytest.raises(ValueError, match="byte limit"):
        validate_manifest(manifest, 1)
    (tmp_path / "link").symlink_to(tmp_path / "weights")
    with pytest.raises(ValueError, match="symlinks"):
        directory_manifest(tmp_path)
