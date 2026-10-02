"""Authentication and authorization at the HTTP boundary.

Every request to the API is authenticated by `AuthMiddleware` (a bearer token from a script
or CI, or the browser's session cookie) and then authorized by `authorize`, which looks the
route up in `POLICY` and checks the caller's role in the project the request is about.

The policy fails closed: a route that needs a project but whose project cannot be determined
is refused, and a test checks that every route of the API is covered.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from anyio import to_thread
from fastapi import Request

from controlplane.api.errors import DomainHttpError
from controlplane.api.session import SESSION_COOKIE, Signer, principal_from_session
from controlplane.application.identity import (
    Authenticator,
    LoginProvider,
    bind_principal,
    require,
    reset_principal,
)
from controlplane.application.jobs import resolve_project
from controlplane.application.projects import UnitOfWorkFactory
from controlplane.domain.access import Principal, ProjectRole
from controlplane.domain.errors import NotFound, PermissionDenied, Unauthenticated

# The caller when authentication is switched off (local development, the demo, tests).
NO_AUTH_PRINCIPAL = Principal(username="anonymous", platform_admin=True)
CSRF_HEADER = "x-mlp-csrf"  # the UI sends it; a cross-site form or image cannot
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class AuthConfig:
    """`authenticator` verifies bearer tokens; `login` + `signer` enable browser sign-in."""

    authenticator: Authenticator
    login: LoginProvider | None = None
    signer: Signer | None = None
    public_url: str = ""  # e.g. https://mlp.example.com, for the sign-in redirect URI
    session_hours: float = 8.0
    secure_cookies: bool = True


# -- authentication --------------------------------------------------------------------

_OPEN_PREFIXES = ("/ui", "/auth/", "/docs", "/redoc")
_OPEN_PATHS = {"/", "/healthz", "/openapi.json", "/docs/oauth2-redirect"}

Scope = MutableMapping[str, Any]
ASGIApp = Callable[
    [Scope, Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]], Awaitable[None]
]


def _is_open(path: str) -> bool:
    return path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES)


class AuthMiddleware:
    """Pure ASGI, so the principal bound here is visible to the endpoint (also when FastAPI
    runs it in a worker thread) and to every audit event the request writes."""

    def __init__(self, app: ASGIApp, config: AuthConfig | None) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            principal = await self._principal(scope)
        except Unauthenticated as exc:
            # The UI reads `sign_in_url` to offer a sign-in button (only when browser sign-in
            # is configured; API-token-only deployments have none).
            sign_in = "/auth/login" if self.config and self.config.login else None
            await _send_error(
                send, 401, "unauthenticated", str(exc), {"www-authenticate": "Bearer"}, sign_in
            )
            return
        except PermissionDenied as exc:
            await _send_error(send, 403, "permission_denied", str(exc))
            return
        scope["mlp.principal"] = principal
        token = bind_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_principal(token)

    async def _principal(self, scope: Scope) -> Principal | None:
        if self.config is None:
            return NO_AUTH_PRINCIPAL
        path: str = scope["path"]
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        authorization = headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            token = authorization[7:].strip()
            return await to_thread.run_sync(self.config.authenticator.authenticate, token)
        principal = self._from_cookie(headers.get("cookie", ""))
        if principal is not None:
            if scope["method"] not in SAFE_METHODS and headers.get(CSRF_HEADER) != "1":
                raise PermissionDenied(
                    f"a browser request that changes something must send {CSRF_HEADER}"
                )
            return principal
        if _is_open(path):
            return None
        raise Unauthenticated("sign in, or send an access token as 'Authorization: Bearer <token>'")

    def _from_cookie(self, cookie_header: str) -> Principal | None:
        if self.config is None or self.config.signer is None:
            return None
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and value:
                data = self.config.signer.loads(value, purpose="session")
                return principal_from_session(data) if data else None
        return None


async def _send_error(
    send: Any,
    status: int,
    code: str,
    message: str,
    extra: dict[str, str] | None = None,
    sign_in_url: str | None = None,
) -> None:
    error: dict[str, str] = {"code": code, "message": message}
    if sign_in_url:
        error["sign_in_url"] = sign_in_url
    body = json.dumps({"error": error}).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    headers += [(k.encode(), v.encode()) for k, v in (extra or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def request_principal(request: Request) -> Principal:
    principal: Principal | None = request.scope.get("mlp.principal")
    if principal is None:
        raise DomainHttpError(Unauthenticated("sign in first"))
    return principal


# -- authorization -----------------------------------------------------------------------

PUBLIC = "public"  # anyone, signed in or not
SIGNED_IN = "signed-in"  # any authenticated caller; no project involved

# (method, route) -> who may call it. Routes not listed: GET needs viewer, anything else
# operator, in the project the route is about.
POLICY: dict[tuple[str, str], str | ProjectRole] = {
    ("GET", "/healthz"): PUBLIC,
    ("GET", "/"): PUBLIC,  # redirects to the UI
    ("GET", "/auth/login"): PUBLIC,
    ("GET", "/auth/callback"): PUBLIC,
    ("POST", "/auth/logout"): PUBLIC,  # checks its own CSRF header
    ("GET", "/me"): SIGNED_IN,
    ("GET", "/projects"): SIGNED_IN,  # lists only the caller's projects
    ("POST", "/projects"): SIGNED_IN,  # the creator becomes the project's admin
    ("DELETE", "/projects/{project_id}"): ProjectRole.ADMIN,
    ("PUT", "/projects/{project}/models/{name}/thresholds"): ProjectRole.ADMIN,
    ("PUT", "/projects/{project}/members/{subject}"): ProjectRole.ADMIN,
    ("DELETE", "/projects/{project}/members/{subject}"): ProjectRole.ADMIN,
}


def required(method: str, route: str) -> str | ProjectRole:
    return POLICY.get(
        (method, route), ProjectRole.VIEWER if method in SAFE_METHODS else ProjectRole.OPERATOR
    )


def project_of(uow: Any, route: str, params: dict[str, str]) -> UUID | None:
    """Which project a request is about, from its path. None: the route names no project."""

    def uuid(key: str) -> UUID:
        try:
            return UUID(params[key])
        except ValueError as exc:
            raise NotFound(key.removesuffix("_id"), params[key]) from exc

    if "project" in params:
        return resolve_project(uow, params["project"]).id
    if "project_id" in params:
        project = uow.projects.get(uuid("project_id"))
        if project is None:
            raise NotFound("project", params["project_id"])
        return project.id  # type: ignore[no-any-return]
    lookups: dict[str, Callable[[], Any]] = {
        "/runs/{run_id}": lambda: uow.runs.get(uuid("run_id")),
        "/pipeline-runs/{run_id}": lambda: uow.pipeline_runs.get(uuid("run_id")),
        "/model-versions/{version_id}": lambda: _model_version_project(uow, uuid("version_id")),
        "/rollouts/{rollout_id}": lambda: _rollout_project(uow, uuid("rollout_id")),
    }
    for prefix, lookup in lookups.items():
        if route == prefix or route.startswith(prefix + "/"):
            found = lookup()
            if found is None:
                raise NotFound(
                    prefix.split("/")[1].rstrip("s").replace("-", " "),
                    next(iter(params.values()), ""),
                )
            return found if isinstance(found, UUID) else found.project_id
    return None


def _model_version_project(uow: Any, version_id: UUID) -> UUID | None:
    version = uow.model_versions.get(version_id)
    model = uow.models.get(version.model_id) if version else None
    return model.project_id if model else None


def _rollout_project(uow: Any, rollout_id: UUID) -> UUID | None:
    rollout = uow.rollouts.get(rollout_id)
    deployment = uow.deployments.get(rollout.deployment_id) if rollout else None
    return deployment.project_id if deployment else None


def authorize(request: Request) -> None:
    """App-wide dependency: may this caller make this request?"""
    route = getattr(request.scope.get("route"), "path", None)
    if route is None:
        return
    rule = required(request.method, route)
    if rule == PUBLIC:
        return
    principal = request_principal(request)
    if rule == SIGNED_IN or principal.platform_admin:
        return
    uow_factory: UnitOfWorkFactory = request.app.state.uow_factory
    try:
        with uow_factory() as uow:
            project_id = project_of(uow, route, dict(request.path_params))
            if project_id is None:  # a route about no project that is not in POLICY: refuse
                raise PermissionDenied("only platform admins may do this")
            require(uow, project_id, principal, ProjectRole(rule))
    except (NotFound, PermissionDenied) as exc:
        raise DomainHttpError(exc) from exc
