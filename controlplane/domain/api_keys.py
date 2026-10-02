"""API keys: how machines outside the platform call a project's public endpoints.

A key is `mlp_live_<id>_<secret>`. The id is public (it names the key in lists, logs and
metrics); the secret is shown once, when the key is issued, and only its SHA-256 is stored.
The secret is 32 random bytes, so a fast hash is enough: there is nothing to brute-force.
The prefix lets secret scanners recognise a leaked key.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Self
from uuid import UUID

from controlplane.domain.entities import validate_slug
from controlplane.domain.errors import InvalidArgument
from controlplane.domain.ids import new_id

KEY_PREFIX = "mlp_live_"
_TOKEN = re.compile(r"^mlp_live_([0-9a-f]{8})_([A-Za-z0-9_-]{20,100})$")
MAX_ENDPOINTS = 50


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def parse_token(token: str) -> tuple[str, str] | None:
    """(key id, secret) if `token` looks like one of our keys, else None."""
    match = _TOKEN.match(token)
    return (match.group(1), match.group(2)) if match else None


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiKey:
    id: UUID = field(default_factory=new_id)
    key_id: str  # the public part, 8 hex characters
    project_id: UUID
    name: str
    endpoints: tuple[str, ...]  # endpoint names this key may call
    units_per_minute: int | None  # None: only the endpoint's own limit applies
    secret_hash: str
    created_by: str
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None

    @classmethod
    def issue(
        cls,
        *,
        project_id: UUID,
        name: str,
        endpoints: tuple[str, ...],
        units_per_minute: int | None,
        expires_at: datetime | None,
        created_by: str,
        now: datetime,
    ) -> tuple[Self, str]:
        """A new key and its full token. The token is never stored: hand it over once."""
        validate_slug(name, "key name")
        if not endpoints or len(endpoints) > MAX_ENDPOINTS:
            raise InvalidArgument(f"a key calls 1 to {MAX_ENDPOINTS} endpoints")
        if units_per_minute is not None and not 1 <= units_per_minute <= 1_000_000:
            raise InvalidArgument("units_per_minute must be between 1 and 1000000")
        if expires_at is not None and expires_at <= now:
            raise InvalidArgument("expires_at must be in the future")
        key_id = secrets.token_hex(4)
        secret = secrets.token_urlsafe(32)
        key = cls(
            key_id=key_id,
            project_id=project_id,
            name=name,
            endpoints=tuple(sorted(set(endpoints))),
            units_per_minute=units_per_minute,
            secret_hash=_digest(secret),
            created_by=created_by,
            created_at=now,
            expires_at=expires_at,
        )
        return key, f"{KEY_PREFIX}{key_id}_{secret}"

    def matches(self, secret: str) -> bool:
        return hmac.compare_digest(self.secret_hash, _digest(secret))

    def usable(self, now: datetime) -> bool:
        return self.revoked_at is None and (self.expires_at is None or now < self.expires_at)

    def may_call(self, endpoint: str) -> bool:
        return endpoint in self.endpoints

    def revoked(self, now: datetime) -> Self:
        return self if self.revoked_at is not None else replace(self, revoked_at=now)

    def used(self, now: datetime) -> Self:
        return replace(self, last_used_at=now)
