"""Standalone KServe storage entrypoint for classic S3 model artifacts."""

import sys

from kserve_storage import Storage

if __name__ == "__main__":
    if len(sys.argv) != 3 or not sys.argv[1].startswith("s3://"):
        raise SystemExit("Expected s3:// model URI and destination directory")
    Storage.download(sys.argv[1], sys.argv[2])
