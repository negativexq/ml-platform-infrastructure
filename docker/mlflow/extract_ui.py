"""Copy the version-matched upstream UI without installing the full MLflow distribution."""

import hashlib
import zipfile
from pathlib import Path

WHEEL = Path("/tmp/mlflow-3.15.0-py3-none-any.whl")
EXPECTED_SHA256 = "3ae54c7f91a6b98ae9360aaeda9b31eb7930571c2690491a7b550c84f795a709"
PREFIX = "mlflow/server/js/build/"
DESTINATION = Path("/ui")

if hashlib.sha256(WHEEL.read_bytes()).hexdigest() != EXPECTED_SHA256:
    raise RuntimeError("upstream MLflow UI wheel checksum mismatch")
with zipfile.ZipFile(WHEEL) as wheel:
    for entry in wheel.infolist():
        if not entry.filename.startswith(PREFIX) or entry.is_dir():
            continue
        destination = DESTINATION / entry.filename.removeprefix(PREFIX)
        if not destination.resolve().is_relative_to(DESTINATION):
            raise RuntimeError("invalid UI archive path")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(wheel.read(entry))
if not (DESTINATION / "index.html").is_file():
    raise RuntimeError("upstream MLflow UI index missing")
