#!/usr/bin/env python3
"""Backup/restore with native PostgreSQL tools and libpq PG* credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# These counts cover durable control-plane identities; credentials are never printed.
TABLES = (
    "schedules",
    "schedule_executions",
    "data_connections",
    "dataset_versions",
    "monitoring_reports",
    "dataset_publication_events",
    "monitoring_rules",
    "monitoring_executions",
    "projects",
    "job_definitions",
    "runs",
    "pipeline_definitions",
    "pipeline_runs",
    "step_runs",
    "models",
    "model_versions",
    "deployments",
    "deployment_revisions",
    "endpoints",
    "api_keys",
    "memberships",
    "audit_events",
    "evaluations",
    "promotions",
    "rollouts",
    "notification_reads",
    "gateway_rate_buckets",
)


def run_tool(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode:
        # libpq stderr may contain connection information: keep it out of logs.
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode}); inspect server logs")
    return result.stdout.strip()


def query(sql: str) -> str:
    return run_tool(["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql])


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def database_required() -> None:
    name = os.environ.get("PGDATABASE", "")
    if not name or name in {"postgres", "template0", "template1"} or "=" in name or "://" in name:
        raise RuntimeError("set PGDATABASE to a dedicated control-plane database name")


def metadata() -> dict[str, Any]:
    tables = json.loads(
        query(
            "SELECT coalesce(json_agg(tablename), '[]'::json) "
            "FROM pg_tables WHERE schemaname='public'"
        )
    )
    counts = {
        table: int(query(f'SELECT count(*) FROM public."{table}"'))
        for table in TABLES
        if table in tables
    }
    heads = query(
        "SELECT version_num FROM public.alembic_version ORDER BY version_num"
    ).splitlines()
    return {"schema_heads": heads, "counts": counts}


def backup(destination: Path) -> None:
    database_required()
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(destination, 0o700)
    try:
        # Native pg_dump takes a consistent snapshot. Counts and heads are derived from
        # that exact snapshot, not separate live queries that can race with writers.
        # Keep the snapshot-exporting session alive while dump and metadata run.
        process = subprocess.Popen(
            ["psql", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY;\nSELECT pg_export_snapshot();\n"
            )
            process.stdin.flush()
            line = process.stdout.readline().strip()
            # psql also prints the BEGIN command tag.
            snapshot = process.stdout.readline().strip() if line == "BEGIN" else line
            if not snapshot or any(c not in "0123456789ABCDEFabcdef-" for c in snapshot):
                raise RuntimeError("could not establish PostgreSQL backup snapshot")
            dump = destination / "database.dump"
            dump.touch(mode=0o600)
            run_tool(
                [
                    "pg_dump",
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--snapshot=" + snapshot,
                    "--file=" + str(dump),
                ]
            )
            # Snapshot identifier is server-generated and validated above.
            sql = (
                "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; "
                f"SET TRANSACTION SNAPSHOT '{snapshot}'; "
                "SELECT json_build_object('schema_heads', "
                "(SELECT json_agg(version_num ORDER BY version_num) FROM alembic_version), "
                "'counts', json_build_object("
                + ",".join(f"'{t}', (SELECT count(*) FROM public.\"{t}\")" for t in TABLES)
                + ")); COMMIT;"
            )
            snapshot_metadata = json.loads(
                run_tool(
                    [
                        "psql",
                        "-X",
                        "-q",
                        "-A",
                        "-t",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-c",
                        sql,
                    ]
                )
            )
            manifest = {
                "format_version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "sha256": digest(dump),
                **snapshot_metadata,
            }
            manifest_path = destination / "manifest.json"
            manifest_path.touch(mode=0o600)
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    except BaseException:
        # A partial backup has no successful manifest; never remove a completed dump.
        (destination / "manifest.json").unlink(missing_ok=True)
        raise


def verify(source: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((source / "manifest.json").read_text())
    if not isinstance(manifest, dict):
        raise RuntimeError("invalid backup manifest")
    if (
        not isinstance(manifest.get("counts"), dict)
        or (
            set(manifest["counts"]) != set(TABLES)
            and not (
                manifest.get("schema_heads") in (["0014"], ["0015"], ["0016"])
                and set(manifest["counts"]) == set(TABLES) - {"gateway_rate_buckets"}
            )
        )
        or any(type(n) is not int or n < 0 for n in manifest["counts"].values())
        or not isinstance(manifest.get("schema_heads"), list)
        or not manifest["schema_heads"]
        or any(not isinstance(h, str) for h in manifest["schema_heads"])
    ):
        raise RuntimeError("invalid backup counts or schema heads")
    if (
        manifest.get("format_version") != 1
        or digest(source / "database.dump") != manifest["sha256"]
    ):
        raise RuntimeError("backup checksum or format does not match")
    run_tool(["pg_restore", "--list", str(source / "database.dump")])
    return manifest


def restore(source: Path, execute: bool) -> None:
    manifest = verify(source)
    if not execute:
        print(
            "Backup checksum and archive verified. Restore requires --execute and an empty target."
        )
        return
    database_required()
    nonempty = int(
        query(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n "
            "ON n.oid=c.relnamespace WHERE n.nspname NOT IN "
            "('pg_catalog','information_schema') AND n.nspname NOT LIKE 'pg_toast%' "
            "AND c.relkind IN ('r','p','v','m','S','f')"
        )
    )
    if nonempty:
        raise RuntimeError("restore target is not empty; use a new database")
    run_tool(
        [
            "pg_restore",
            "--exit-on-error",
            "--single-transaction",
            "--no-owner",
            "--no-privileges",
            "--dbname=" + os.environ["PGDATABASE"],
            str(source / "database.dump"),
        ]
    )
    restored = metadata()
    if restored != {k: manifest[k] for k in ("schema_heads", "counts")}:
        raise RuntimeError("restored schema heads or row counts do not match the snapshot")
    print("Restore verified: schema heads and durable table counts match the snapshot.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("backup").add_argument("destination", type=Path)
    restore_parser = commands.add_parser("restore")
    restore_parser.add_argument("source", type=Path)
    restore_parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "backup":
            backup(args.destination)
            print("Backup completed with snapshot counts and checksum.")
        else:
            restore(args.source, args.execute)
    except (RuntimeError, OSError, ValueError, KeyError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
