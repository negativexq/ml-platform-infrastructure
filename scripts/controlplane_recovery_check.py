#!/usr/bin/env python3
"""Native PostgreSQL snapshot/restore evidence; no cluster/container startup.

Libpq environment identifies the source DB. --target names a pre-created EMPTY isolated
DB on the same server. Access/history/lineage/budget rows are hashed, never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any

from controlplane_backup import TABLES, backup, metadata, query, restore


def fingerprints() -> dict[str, str]:
    return {
        table: query(
            "SELECT md5(coalesce(string_agg(md5(row_to_json(t)::text), '' "
            f"ORDER BY md5(row_to_json(t)::text)), '')) FROM public.\"{table}\" t"
        )
        for table in TABLES
    }


def drill(destination: Path, target: str, source_quiescent: bool) -> None:
    source = os.environ.get("PGDATABASE", "")
    if (
        not source
        or source == target
        or not target
        or target in {"postgres", "template0", "template1"}
    ):
        raise ValueError("source and dedicated empty recovery DB must be different")
    if not source_quiescent:
        raise ValueError("stop source writers before the equality drill")
    if destination.exists():
        raise ValueError("drill directory must not already exist")
    destination.mkdir(parents=True, mode=0o700)
    started = time.monotonic()
    report: dict[str, Any] = {"passed": False, "started_at_epoch": time.time()}
    try:
        before = fingerprints()
        backup(destination / "backup")
        report["backup_seconds"] = time.monotonic() - started
        # Check no writers changed the source outside the snapshot transaction.
        if fingerprints() != before:
            raise RuntimeError("source changed during backup; quiesce writers and retry")
        os.environ["PGDATABASE"] = target
        restore_started = time.monotonic()
        restore(destination / "backup", True)
        report["restore_verify_seconds"] = time.monotonic() - restore_started
        if fingerprints() != before:
            raise RuntimeError("restored durable row fingerprints differ")
        report["metadata"] = metadata()
        report["durable_rows_equal"] = True
        report["passed"] = True
        report["database_recovery_seconds"] = time.monotonic() - started
        report["limits"] = (
            "Database drill only; service/OIDC/workload recovery and platform RPO/RTO unproven"
        )
        (destination / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print("Database recovery drill passed:", destination / "report.json")
    finally:
        os.environ["PGDATABASE"] = source
        if not report["passed"]:
            (destination / "report.json").write_text(json.dumps(report, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--source-quiescent", action="store_true", required=True)
    args = parser.parse_args()
    drill(args.out, args.target, args.source_quiescent)


if __name__ == "__main__":
    main()
