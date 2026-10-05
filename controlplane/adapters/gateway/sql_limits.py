"""Shared transactional buckets. PostgreSQL is production; SQLite supports local gates."""

from __future__ import annotations

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
        postgres = self._engine.dialect.name == "postgresql"
        with self._engine.connect() as conn:
            if postgres:
                conn.begin()
                conn.execute(text("SET LOCAL lock_timeout = '2s'"))
                conn.execute(text("SET LOCAL statement_timeout = '3s'"))
            else:
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
                    text(
                        "SELECT tokens, updated_at FROM gateway_rate_buckets WHERE name=:name"
                        + (" FOR UPDATE" if postgres else "")
                    ),
                    {"name": name},
                ).one()
                rows[name] = (float(row[0]), float(row[1]))
            # Read the shared DB clock after acquiring all locks.
            now = (
                float(conn.scalar(text("SELECT extract(epoch FROM clock_timestamp())")))
                if postgres
                else time.time()
            )
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
