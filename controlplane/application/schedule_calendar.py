"""Five-field cron, local wall-clock semantics, nonexistent=skip, ambiguous=first."""

import re
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterBadCronError, CroniterBadDateError, croniter

from controlplane.domain.errors import InvalidArgument


def next_occurrence(expression: str, timezone: str, after: datetime) -> datetime:
    if after.tzinfo is None:
        raise InvalidArgument("schedule timestamps must have a timezone")
    if len(expression) > 128 or len(expression.split()) != 5:
        raise InvalidArgument("cron must have five fields: minute hour day month weekday")
    # Reject randomized/hash and extended cron syntax: previews and dispatch must agree.
    standard = re.sub(
        r"\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC|MON|TUE|WED|THU|FRI|SAT|SUN)\b",
        "1",
        expression.upper(),
    )
    if not re.fullmatch(r"[0-9*/ ,\-]+", standard):
        raise InvalidArgument("cron must use deterministic, standard five-field syntax")
    try:
        zone = ZoneInfo(timezone)
        cursor = croniter(
            expression, after.astimezone(zone).replace(tzinfo=None), max_years_between_matches=50
        )
        for _ in range(10000):
            wall = cast(datetime, cursor.get_next(datetime))
            candidate = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
            # Round-trip rejects spring-forward gaps; fold=0 rejects fall-back duplicates.
            if candidate > after and candidate.astimezone(zone).replace(tzinfo=None) == wall:
                return candidate
    except (ZoneInfoNotFoundError, CroniterBadCronError, CroniterBadDateError, ValueError) as exc:
        raise InvalidArgument("invalid cron/timezone or no execution within 50 years") from exc
    raise InvalidArgument("cron exceeds the timezone calculation limit")


def preview(expression: str, timezone: str, after: datetime, count: int = 5) -> list[datetime]:
    dates = []
    for _ in range(count):
        after = next_occurrence(expression, timezone, after)
        dates.append(after)
    return dates
