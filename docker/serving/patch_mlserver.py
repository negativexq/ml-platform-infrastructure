"""Build the explicit MLServer compatibility fork used by the serving image.

Upstream 1.7.1 caps FastAPI below the Starlette security fixes. This fork changes
dependency metadata only; inference code is unchanged. Runtime compatibility is
verified separately by the serving acceptance gate. Never bypass pip check.
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import urllib.request
import zipfile
from pathlib import Path

SOURCE_URL = (
    "https://files.pythonhosted.org/packages/34/44/"
    "1982c0ed416578ee09285ee7b22dbb1da87c4ae154edd39fe914984c549d/"
    "mlserver-1.7.1-py3-none-any.whl"
)
SOURCE_SHA256 = "d2ac7502915bb5311343a878aa1fb614f9b0f2436ee550a6137c1872a4dca193"
VERSION = "1.7.1+mlp.1"


def build(source: bytes, output: Path) -> Path:
    if hashlib.sha256(source).hexdigest() != SOURCE_SHA256:
        raise ValueError("MLServer source wheel checksum mismatch")
    old_info = "mlserver-1.7.1.dist-info/"
    new_info = f"mlserver-{VERSION}.dist-info/"
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(source)) as wheel:
        for name in wheel.namelist():
            if name.endswith(("RECORD.jws", "RECORD.p7s")):
                raise ValueError("Refusing to patch a signed wheel")
            if name == old_info + "RECORD":
                continue
            data = wheel.read(name)
            if name == old_info + "METADATA":
                metadata = data.decode()
                old = "Requires-Dist: fastapi (>=0.88.0,!=0.89.0,<0.116.0)"
                if metadata.count(old) != 1 or metadata.count("Version: 1.7.1\n") != 1:
                    raise ValueError("Unexpected upstream MLServer dependency metadata")
                metadata = metadata.replace("Version: 1.7.1\n", f"Version: {VERSION}\n")
                metadata = metadata.replace(
                    old,
                    "Requires-Dist: fastapi (>=0.142.2,<1)\n"
                    "Requires-Dist: starlette (>=1.3.1,<2)",
                )
                data = metadata.encode()
            files[name.replace(old_info, new_info, 1)] = data
    records = io.StringIO(newline="")
    writer = csv.writer(records)
    for name, data in sorted(files.items()):
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        writer.writerow((name, "sha256=" + digest, len(data)))
    writer.writerow((new_info + "RECORD", "", ""))
    files[new_info + "RECORD"] = records.getvalue().encode()
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"mlserver-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 10, 6, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            wheel.writestr(info, data)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as response:
        source = response.read()
    print(build(source, args.out))
