"""Shared transactional buckets. PostgreSQL is production; SQLite supports local gates."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable, Sequence

from sqlalchemy import Engine, text

from controlplane.application.gateway import Allowance


class SqlTokenBucketLimiter:
    def __init__(
        self, engine: Engine, *, observe: Callable[[str, str, float], None] | None = None
    ) -> None:
        if engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("unsupported rate-limit database")
        self._engine = engine
        self._observe = observe or (lambda operation, outcome, seconds: None)

    def take(self, buckets: Sequence[tuple[str, int]], units: int) -> Allowance:
        return self._measured(buckets, units, "take")

    def admit(self, buckets: Sequence[tuple[str, int]]) -> Allowance:
        return self._measured(buckets, 0, "admit")

    def refund(self, buckets: Sequence[tuple[str, int]], units: int) -> None:
        self._measured(buckets, units, "refund")

    def charge(self, buckets: Sequence[tuple[str, int]], units: int) -> None:
        self._measured(buckets, units, "charge")

    def _measured(
        self, buckets: Sequence[tuple[str, int]], units: int, operation: str
    ) -> Allowance:
        started, outcome = time.monotonic(), "error"
        try:
            allowance = self._operate(buckets, units, operation)
            outcome = "allowed" if allowance.allowed else "denied"
            return allowance
        finally:
            self._observe(operation, outcome, time.monotonic() - started)

    def _operate(self, buckets: Sequence[tuple[str, int]], units: int, operation: str) -> Allowance:
        if not buckets or units < 0 or any(limit <= 0 for _, limit in buckets):
            raise ValueError("invalid rate-limit operation")
        if len({name for name, _ in buckets}) != len(buckets):
            raise ValueError("duplicate rate-limit bucket")
        ordered = sorted(buckets)
        if self._engine.dialect.name == "postgresql":
            return self._postgres(ordered, units, operation)
        with self._engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            # Ordered upserts and row locks avoid cross-bucket lock inversion.
            for name, limit in ordered:
                conn.execute(
                    text(
                        "INSERT INTO gateway_rate_buckets (name, tokens, updated_at) "
                        "VALUES (:name, :tokens, 0) ON CONFLICT (name) DO NOTHING"
                    ),
                    {"name": name, "tokens": float(limit)},
                )
            rows = {}
            for name, _ in ordered:
                row = conn.execute(
                    text("SELECT tokens, updated_at FROM gateway_rate_buckets WHERE name=:name"),
                    {"name": name},
                ).one()
                rows[name] = (float(row[0]), float(row[1]))
            # Read the shared DB clock after acquiring all locks.
            now = time.time()
            levels = {
                name: min(float(limit), rows[name][0] + max(0, now - rows[name][1]) * limit / 60)
                for name, limit in ordered
            }
            cost = units if operation == "take" else 1
            short = [(name, limit) for name, limit in ordered if levels[name] < cost]
            allowed = not short if operation in {"take", "admit"} else True
            for name, limit in ordered:
                level = levels[name]
                if operation == "take" and allowed or operation == "charge":
                    level -= units
                elif operation == "refund":
                    level = min(float(limit), level + units)
                conn.execute(
                    text(
                        "UPDATE gateway_rate_buckets SET tokens=:tokens, updated_at=:now "
                        "WHERE name=:name"
                    ),
                    {"tokens": level, "now": now, "name": name},
                )
                levels[name] = level
            conn.commit()
            if not allowed:
                name, limit = max(short, key=lambda b: (cost - levels[b[0]]) / b[1])
                return Allowance(False, limit, 0, math.ceil((cost - levels[name]) * 60 / limit))
            name, limit = min(ordered, key=lambda b: levels[b[0]])
            return Allowance(
                True,
                limit,
                max(0, int(levels[name])),
                math.ceil((limit - levels[name]) * 60 / limit),
            )

    def _postgres(self, ordered: list[tuple[str, int]], units: int, operation: str) -> Allowance:
        # Do refill, admission and updates in one statement while holding ordered row
        # locks. The clock depends on the fully materialized lock set, so time spent
        # waiting for a hot bucket is included in its refill. No process-local budget.
        cost = units if operation == "take" else 1
        parameters = {
            "buckets": json.dumps([{"name": name, "capacity": limit} for name, limit in ordered]),
            "units": units,
            "cost": cost,
            "operation": operation,
            "count": len(ordered),
        }
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    "SELECT set_config('lock_timeout', '2s', true), "
                    "set_config('statement_timeout', '3s', true)"
                )
            )
            # Ordered first-use inserts avoid lock inversion even for absent buckets.
            conn.execute(
                text(
                    "INSERT INTO gateway_rate_buckets (name, tokens, updated_at) "
                    "SELECT name, capacity, 0 FROM "
                    "jsonb_to_recordset(CAST(:buckets AS jsonb)) AS x(name text, capacity float8) "
                    'ORDER BY name COLLATE "C" ON CONFLICT (name) DO NOTHING'
                ),
                parameters,
            )
            rows = conn.execute(
                text(
                    """
                    WITH input AS MATERIALIZED (
                        SELECT * FROM jsonb_to_recordset(CAST(:buckets AS jsonb))
                        AS x(name text, capacity float8)
                    ), locked AS MATERIALIZED (
                        SELECT b.name, b.tokens, b.updated_at FROM gateway_rate_buckets b
                        JOIN input i ON i.name=b.name ORDER BY b.name COLLATE "C" FOR UPDATE OF b
                    ), clock AS MATERIALIZED (
                        SELECT extract(epoch FROM clock_timestamp())::float8 AS now
                        FROM (SELECT count(*) AS n FROM locked) barrier WHERE n=:count
                    ), levels AS MATERIALIZED (
                        SELECT l.name, i.capacity,
                            LEAST(i.capacity, l.tokens +
                                GREATEST(0, c.now-l.updated_at)*i.capacity/60) AS tokens
                        FROM locked l JOIN input i ON i.name=l.name CROSS JOIN clock c
                    ), decision AS MATERIALIZED (
                        SELECT CASE WHEN :operation IN ('take', 'admit')
                            THEN bool_and(tokens >= :cost) ELSE true END AS allowed FROM levels
                    )
                    UPDATE gateway_rate_buckets b SET
                        tokens=CASE
                            WHEN (:operation='take' AND d.allowed) OR :operation='charge'
                                THEN l.tokens-:units
                            WHEN :operation='refund' THEN LEAST(l.capacity, l.tokens+:units)
                            ELSE l.tokens END,
                        updated_at=c.now
                    FROM levels l CROSS JOIN decision d CROSS JOIN clock c
                    WHERE b.name=l.name
                    RETURNING b.name, b.tokens, l.capacity, d.allowed
                    """
                ),
                parameters,
            ).all()
            if len(rows) != len(ordered):
                raise RuntimeError("rate-limit bucket set incomplete")
        levels = {row[0]: float(row[1]) for row in rows}
        allowed = bool(rows[0][3])
        if not allowed:
            short = [(name, limit) for name, limit in ordered if levels[name] < cost]
            name, limit = max(short, key=lambda b: (cost - levels[b[0]]) / b[1])
            return Allowance(False, limit, 0, math.ceil((cost - levels[name]) * 60 / limit))
        name, limit = min(ordered, key=lambda b: levels[b[0]])
        return Allowance(
            True, limit, max(0, int(levels[name])), math.ceil((limit - levels[name]) * 60 / limit)
        )
