"""Bounded parameter contracts. No references, shell expansion or secret values in audit."""

import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError

from controlplane.domain.errors import InvalidArgument

MAX_BYTES = 65536
KEYWORDS = {
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "default",
    "description",
    "title",
    "format",
}
FORMATS = {"date", "date-time", "uri"}
CHECKER = FormatChecker(formats=[])


def valid_date(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    try:
        from datetime import date

        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def valid_datetime(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    try:
        return (
            "T" in value and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
        )
    except ValueError:
        return False


def valid_uri(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    try:
        parsed = urlsplit(value)
        return (
            bool(parsed.scheme)
            and not any(c.isspace() for c in value)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        return False


CHECKER.checks("date")(valid_date)
CHECKER.checks("date-time")(valid_datetime)
CHECKER.checks("uri")(valid_uri)


def check_depth(value: Any, depth: int = 0) -> None:
    if depth > 16:
        raise InvalidArgument("parameters/schema nesting must not exceed 16 levels")
    if isinstance(value, dict):
        for item in value.values():
            check_depth(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            check_depth(item, depth + 1)


def encode(value: Any) -> str:
    check_depth(value)
    try:
        result = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidArgument("parameters must be finite JSON values") from exc
    if len(result.encode()) > MAX_BYTES:
        raise InvalidArgument("parameters/schema must not exceed 64 KiB")
    return result


def validate_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    result = cast(dict[str, Any], json.loads(encode(dict(schema))))
    if not result:
        return {}
    if result.get("type") != "object" or result.get("additionalProperties") is not False:
        raise InvalidArgument("parameter_schema must be an object with additionalProperties=false")

    def visit(node: Any, depth: int) -> None:
        if depth > 6 or not isinstance(node, dict):
            raise InvalidArgument("parameter_schema must use bounded object schemas (depth <= 6)")
        if set(node) - KEYWORDS:
            raise InvalidArgument(
                "unsupported parameter schema keywords; references/patterns are disabled"
            )
        if node.get("format") and node["format"] not in FORMATS:
            raise InvalidArgument("parameter format must be date, date-time or uri")
        properties = node.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > 64:
            raise InvalidArgument("parameter_schema allows at most 64 properties per object")
        for key, prop in properties.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 64
                or not key.replace("_", "a").replace("-", "a").isalnum()
            ):
                raise InvalidArgument(
                    "parameter names must be 1-64 letters, digits, underscores or dashes"
                )
            visit(prop, depth + 1)
        if "items" in node:
            visit(node["items"], depth + 1)
        if isinstance(node.get("additionalProperties"), dict):
            visit(node["additionalProperties"], depth + 1)

    visit(result, 0)
    try:
        Draft202012Validator.check_schema(result)
    except SchemaError as exc:
        raise InvalidArgument("invalid parameter_schema") from exc
    for name, prop in result.get("properties", {}).items():
        if "default" in prop and not Draft202012Validator(prop, format_checker=CHECKER).is_valid(
            prop["default"]
        ):
            raise InvalidArgument(f"invalid default for parameter {name!r}")
    return result


def resolve(schema: Mapping[str, Any], values: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = validate_schema(schema)
    result = cast(dict[str, Any], json.loads(encode(dict(values or {}))))
    if not normalized:
        if result:
            raise InvalidArgument("this definition declares no parameters")
        return {}
    for name, prop in normalized.get("properties", {}).items():
        if name not in result and "default" in prop:
            result[name] = deepcopy(prop["default"])
    encode(result)
    error = next(Draft202012Validator(normalized, format_checker=CHECKER).iter_errors(result), None)
    if error:
        location = ".".join(str(part) for part in error.absolute_path) or "parameters"
        # Do not echo values in validation failures, logs or audit events.
        raise InvalidArgument(f"{location}: parameter validation failed ({error.validator})")
    return result


def fingerprint(values: Mapping[str, Any]) -> str:
    return sha256(encode(dict(values)).encode()).hexdigest()


def scheduled_values(
    values: Mapping[str, Any], bindings: Mapping[str, str], at: datetime, timezone: str
) -> dict[str, Any]:
    result = deepcopy(dict(values))
    sources = {
        "scheduled_for": at.astimezone(UTC).isoformat(),
        "processing_date": at.astimezone(ZoneInfo(timezone)).date().isoformat(),
    }
    if set(values) & set(bindings):
        raise InvalidArgument("schedule parameters and parameter_bindings cannot overlap")
    for name, source in bindings.items():
        if source not in sources:
            raise InvalidArgument("parameter binding must be scheduled_for or processing_date")
        result[name] = sources[source]
    return result


def project_values(schema: Mapping[str, Any], values: Mapping[str, Any]) -> dict[str, Any]:
    """Pass only the parameters declared by this immutable step definition."""
    names = schema.get("properties", {})
    return resolve(schema, {key: value for key, value in values.items() if key in names})
