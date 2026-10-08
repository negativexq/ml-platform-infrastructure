"""Content identity of a local MLflow model package; standard library only."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


def validate_manifest(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 1000:
        raise ValueError("model manifest requires 1-1000 files")
    files: dict[str, dict[str, Any]] = {}
    total = 0
    for entry in value:
        if (
            not isinstance(entry, dict)
            or not {"path", "size", "sha256"} <= set(entry)
            or set(entry) - {"path", "size", "sha256", "object_version_id"}
        ):
            raise ValueError("invalid model manifest entry")
        path, size, digest = entry["path"], entry["size"], entry["sha256"]
        if not isinstance(path, str) or not path or len(path) > 1024:
            raise ValueError("invalid model artifact path")
        parts = PurePosixPath(path)
        if (
            parts.is_absolute()
            or ".." in parts.parts
            or "\\" in path
            or "\x00" in path
            or str(parts) != path
            or path == "."
            or path in files
        ):
            raise ValueError("unsafe or duplicate model artifact path")
        if (
            type(size) is not int
            or size < 0
            or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)
        ):
            raise ValueError("model files require byte sizes and lowercase SHA-256 hashes")
        version = entry.get("object_version_id")
        if version is not None and (
            not isinstance(version, str) or not version or version == "null" or len(version) > 1024
        ):
            raise ValueError("invalid model object version")
        files[path] = {
            "path": path,
            "size": size,
            "sha256": digest,
            **({"object_version_id": version} if version else {}),
        }
        total += size
        if total > limit:
            raise ValueError("model manifest exceeds artifact byte limit")
    if "MLmodel" not in files:
        raise ValueError("model manifest must include MLmodel")
    for path in files:
        if any(str(parent) in files for parent in PurePosixPath(path).parents):
            raise ValueError("model artifact file conflicts with a directory")
    return [files[path] for path in sorted(files)]


def directory_manifest(root: Path) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError("model package must not contain symlinks")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return validate_manifest(entries, 1024**3)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Hash a trusted, complete local MLflow model package"
    )
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(directory_manifest(args.directory), indent=2))
