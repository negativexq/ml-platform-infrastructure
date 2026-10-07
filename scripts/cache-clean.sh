#!/usr/bin/env bash
# Rebuildable caches only; images, containers, volumes and installed environments stay.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MLP_CACHE_LIMIT="${MLP_CACHE_LIMIT:-2GB}"
MLP_PYTHON="${MLP_PYTHON:-$ROOT/.venv/bin/python}"
MLP_UV="${MLP_UV:-uv}"
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  docker buildx prune --force --max-used-space "$MLP_CACHE_LIMIT"
fi
if [[ -x "$MLP_PYTHON" ]]; then
  "$MLP_PYTHON" -m pip cache purge
fi
if command -v "$MLP_UV" >/dev/null; then
  "$MLP_UV" cache clean
fi
if command -v go >/dev/null; then
  go clean -cache -modcache
fi
if [[ -x "$MLP_PYTHON" ]]; then
  "$MLP_PYTHON" - "$ROOT" <<'PY'
import pathlib
import shutil
import sys

root = pathlib.Path(sys.argv[1])
for name in (".pytest_cache", ".mypy_cache", ".ruff_cache"):
    path = root / name
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
PY
fi
df -h "$ROOT"
