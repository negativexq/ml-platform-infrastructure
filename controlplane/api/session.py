"""Signed cookies for the browser session and the in-flight sign-in.

`base64url(json).base64url(hmac-sha256)`: tamper-evident and self-contained, so the API
stays stateless and any replica can serve any request. Not encrypted: it holds only who the
user is (name, groups), never a token.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from controlplane.domain.access import Principal

SESSION_COOKIE = "mlp_session"
SESSION_PURPOSE = "session-v2"
MAX_SESSION_SECONDS = 900
MAX_ADMIN_SESSION_SECONDS = 300
MAX_COOKIE_BYTES = 3800
LOGIN_COOKIE = "mlp_login"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("the session secret must be at least 32 characters")
        self._key = hashlib.sha256(secret.encode()).digest()

    def _mac(self, payload: str, purpose: str) -> str:
        return _b64(hmac.new(self._key, f"{purpose}.{payload}".encode(), hashlib.sha256).digest())

    def dumps(self, data: dict[str, Any], *, purpose: str, ttl_seconds: float) -> str:
        payload = _b64(json.dumps({**data, "exp": int(time.time() + ttl_seconds)}).encode())
        return f"{payload}.{self._mac(payload, purpose)}"

    def loads(self, value: str, *, purpose: str) -> dict[str, Any] | None:
        """The data, or None if the value was tampered with, is for another purpose or expired."""
        payload, _, mac = value.partition(".")
        if not mac or not hmac.compare_digest(mac, self._mac(payload, purpose)):
            return None
        try:
            data: dict[str, Any] = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < time.time():
            return None
        return data


def principal_to_session(p: Principal) -> dict[str, Any]:
    return {
        "u": p.username,
        "g": list(p.groups),
        "e": p.email,
        "n": p.display_name,
        "a": p.platform_admin,
    }


def principal_from_session(data: dict[str, Any]) -> Principal | None:
    try:
        return Principal(
            username=str(data["u"]),
            groups=tuple(str(g) for g in data.get("g", [])),
            email=data.get("e"),
            display_name=data.get("n"),
            platform_admin=bool(data.get("a")),
        )
    except (KeyError, TypeError):
        return None
