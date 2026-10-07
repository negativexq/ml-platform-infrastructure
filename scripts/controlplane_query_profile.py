#!/usr/bin/env python3
"""Install/check pg_stat_statements and report query IDs, never SQL text.

CP_PROFILE_DATABASE_URL is an operator credential, not a runtime application role.
Installation requires preloading the extension and a PostgreSQL restart beforehand.
"""

from __future__ import annotations

import argparse
import json
import os

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


def profile(url: str, *, install: bool = False, limit: int = 20) -> list[dict[str, object]]:
    with (
        psycopg.connect(
            url.replace("postgresql+psycopg://", "postgresql://", 1), row_factory=dict_row
        ) as conn,
        conn.cursor() as cursor,
    ):
        if install:
            cursor.execute("SHOW shared_preload_libraries")
            preloaded = cursor.fetchone()
            if not preloaded or "pg_stat_statements" not in {
                x.strip() for x in str(preloaded["shared_preload_libraries"]).split(",")
            }:
                raise RuntimeError("Preload pg_stat_statements and restart PostgreSQL first")
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_stat_statements")
        cursor.execute("""
                SELECT n.nspname FROM pg_extension e
                JOIN pg_namespace n ON n.oid = e.extnamespace
                WHERE e.extname = 'pg_stat_statements'
            """)
        extension = cursor.fetchone()
        if extension is None:
            raise RuntimeError("An operator must run --install in this database first")
        cursor.execute(
            sql.SQL("""
                SELECT userid, dbid, queryid::text, calls, rows,
                       total_exec_time, mean_exec_time, max_exec_time,
                       shared_blks_hit, shared_blks_read, temp_blks_written
                FROM {}.pg_stat_statements
                WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
                  AND queryid IS NOT NULL
                ORDER BY total_exec_time DESC LIMIT %s
            """).format(sql.Identifier(extension["nspname"])),
            (limit,),
        )
        return list(cursor.fetchall())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    url = os.environ.get("CP_PROFILE_DATABASE_URL")
    if not url:
        parser.error("Set CP_PROFILE_DATABASE_URL to a dedicated operator/monitoring DSN")
    try:
        print(
            json.dumps(
                {
                    "unit": "ms",
                    "cumulative_since_reset": True,
                    "queries": profile(url, install=args.install, limit=args.limit),
                },
                indent=2,
            )
        )
    except (psycopg.Error, RuntimeError) as exc:
        # Driver errors may contain SQL/credentials; only the controlled precondition is printed.
        parser.exit(1, f"{exc if isinstance(exc, RuntimeError) else type(exc).__name__}\n")


if __name__ == "__main__":
    main()
