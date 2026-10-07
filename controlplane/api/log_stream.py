"""Authorize a short-lived, pod-bound capability; streaming belongs to the Go service."""

import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response
from pydantic import BaseModel

from controlplane.application.providers import WorkflowProvider


class LogStreamTicket(BaseModel):
    url: str
    token: str
    expires_at: int


def configure(key: str, url: str) -> tuple[str, str]:
    if not key:
        return "", url
    if len(key.encode()) < 32:
        raise ValueError("log streaming signing key must contain at least 32 bytes")
    parts = urlsplit(url)
    local = parts.hostname in {"127.0.0.1", "localhost", "::1"}
    relative = url.startswith("/") and not url.startswith("//") and not parts.netloc
    if not (
        relative
        or (parts.netloc and parts.scheme == "https")
        or (parts.netloc and parts.scheme == "http" and local)
    ):
        raise ValueError("log streaming URL requires a relative path, HTTPS or loopback HTTP")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError("log streaming URL must not contain credentials, query or fragment")
    return key, url


def ticket(request: Request, response: Response, ref: str | None, step: str) -> LogStreamTicket:
    key, url = request.app.state.log_stream
    workflow: WorkflowProvider | None = request.app.state.workflow
    if not key or workflow is None:
        raise HTTPException(503, "log streaming is not configured")
    if ref is None:
        raise HTTPException(409, "workload has not started")
    target = workflow.get_log_target(ref, step)
    if target is None:
        raise HTTPException(409, "workload pod has not started")
    now = int(time.time())
    claims = {
        "v": 1,
        "namespace": target.namespace,
        "pod": target.pod,
        "pod_uid": target.pod_uid,
        "workflow": target.workflow,
        "workflow_uid": target.workflow_uid,
        "container": "main",
        "iat": now,
        "exp": now + 60,
        "end": now + 300,
        "nonce": secrets.token_hex(16),
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode()).rstrip(
        b"="
    )
    signature = base64.urlsafe_b64encode(hmac.digest(key.encode(), payload, hashlib.sha256)).rstrip(
        b"="
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return LogStreamTicket(
        url=url, token=(payload + b"." + signature).decode(), expires_at=now + 60
    )
