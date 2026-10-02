"""Who am I, who is in a project, and browser sign-in."""

from __future__ import annotations

import base64
import hashlib
import secrets
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from anyio import to_thread
from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict

from controlplane.api.auth import AuthConfig, request_principal
from controlplane.api.errors import DomainHttpError, PlatformRoute
from controlplane.api.schemas import ErrorOut
from controlplane.api.session import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    principal_to_session,
)
from controlplane.application.identity import role_in
from controlplane.application.members import MembershipService
from controlplane.application.projects import UnitOfWorkFactory
from controlplane.domain.access import Membership, ProjectRole
from controlplane.domain.errors import Unauthenticated

_ERRORS: dict[int | str, dict[str, object]] = {
    401: {"model": ErrorOut},
    403: {"model": ErrorOut},
    404: {"model": ErrorOut},
    409: {"model": ErrorOut},
    422: {"model": ErrorOut},
}


class MeOut(BaseModel):
    username: str
    display_name: str | None
    email: str | None
    groups: list[str]
    platform_admin: bool
    auth: Literal["oidc", "none"]
    can_sign_out: bool
    roles: dict[str, ProjectRole]  # project name -> the caller's role there


class MemberOut(BaseModel):
    subject: str
    kind: Literal["user", "group"]
    name: str
    role: ProjectRole
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, m: Membership) -> MemberOut:
        kind, _, name = m.subject.partition(":")
        return cls(
            subject=m.subject,
            kind=kind,
            name=name,
            role=m.role,
            created_at=m.created_at,
            updated_at=m.updated_at,
        )


class MemberList(BaseModel):
    items: list[MemberOut]


class MemberRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: ProjectRole


def identity_router() -> APIRouter:
    router = APIRouter(route_class=PlatformRoute, tags=["identity"])

    def members(request: Request) -> MembershipService:
        service: MembershipService = request.app.state.members
        return service

    @router.get(
        "/me",
        response_model=MeOut,
        responses=_ERRORS,
        summary="Who the caller is, and their role in each project",
    )
    def me(request: Request) -> MeOut:
        principal = request_principal(request)
        auth: AuthConfig | None = request.app.state.auth
        uow_factory: UnitOfWorkFactory = request.app.state.uow_factory
        with uow_factory() as uow:
            if principal.platform_admin:
                projects = uow.projects.list(limit=10_000, offset=0)
                roles = {p.name: ProjectRole.ADMIN for p in projects}
            else:
                roles = {}
                for m in uow.memberships.list_for_subjects(principal.subjects):
                    project = uow.projects.get(m.project_id)
                    role = role_in(uow, m.project_id, principal)
                    if project is not None and role is not None:
                        roles[project.name] = role
        return MeOut(
            username=principal.username,
            display_name=principal.display_name,
            email=principal.email,
            groups=list(principal.groups),
            platform_admin=principal.platform_admin,
            auth="none" if auth is None else "oidc",
            can_sign_out=auth is not None and auth.login is not None,
            roles=roles,
        )

    @router.get(
        "/projects/{project}/members",
        response_model=MemberList,
        responses=_ERRORS,
        summary="Members of a project",
    )
    def list_members(project: str, request: Request) -> MemberList:
        return MemberList(items=[MemberOut.from_domain(m) for m in members(request).list(project)])

    @router.put(
        "/projects/{project}/members/{subject}",
        response_model=MemberOut,
        responses=_ERRORS,
        summary="Grant a role to a user (user:<name>) or group (group:<name>), or change it",
    )
    def set_member(
        project: str, subject: str, body: MemberRoleIn, request: Request, response: Response
    ) -> MemberOut:
        membership, changed = members(request).set_role(project, subject, body.role)
        if not changed:
            response.status_code = status.HTTP_200_OK
        return MemberOut.from_domain(membership)

    @router.delete(
        "/projects/{project}/members/{subject}",
        status_code=status.HTTP_204_NO_CONTENT,
        responses=_ERRORS,
        summary="Remove a member (a project keeps at least one admin)",
    )
    def remove_member(project: str, subject: str, request: Request) -> Response:
        members(request).remove(project, subject)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return router


# -- browser sign-in (authorization code + PKCE, run by the server) ---------------------------


def _safe_next(target: str | None) -> str:
    """Only ever send the browser back into the UI on this host."""
    if target and target.startswith("/ui") and not urlsplit(target).netloc and "\\" not in target:
        return target
    return "/ui/"


def login_router(config: AuthConfig) -> APIRouter:
    router = APIRouter(prefix="/auth", tags=["identity"], include_in_schema=False)
    assert config.login is not None and config.signer is not None
    login, signer = config.login, config.signer

    def base_url(request: Request) -> str:
        return (config.public_url or str(request.base_url)).rstrip("/")

    def cookie(response: Response, name: str, value: str, max_age: int) -> None:
        response.set_cookie(
            name,
            value,
            max_age=max_age,
            httponly=True,
            samesite="lax",
            secure=config.secure_cookies,
            path="/",
        )

    @router.get("/login")
    def start(request: Request, next: str | None = Query(default=None)) -> RedirectResponse:  # noqa: A002
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        url = login.authorization_url(
            redirect_uri=f"{base_url(request)}/auth/callback",
            state=state,
            nonce=nonce,
            code_challenge=challenge,
        )
        response = RedirectResponse(url, status_code=status.HTTP_302_FOUND)
        cookie(
            response,
            LOGIN_COOKIE,
            signer.dumps(
                {"s": state, "n": nonce, "v": verifier, "next": _safe_next(next)},
                purpose="login",
                ttl_seconds=600,
            ),
            600,
        )
        return response

    @router.get("/callback")
    async def callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
        error_description: str | None = None,
    ) -> Response:
        pending = signer.loads(request.cookies.get(LOGIN_COOKIE, ""), purpose="login")
        if error:
            return _sign_in_failed(
                f"{error}: {error_description or 'the identity provider refused the sign-in'}"
            )
        if pending is None or not code or not state or state != pending.get("s"):
            return _sign_in_failed("this sign-in expired or did not start here; try again")
        try:
            principal, id_token = await to_thread.run_sync(
                lambda: login.complete(
                    code=code,
                    redirect_uri=f"{base_url(request)}/auth/callback",
                    code_verifier=pending["v"],
                    nonce=pending["n"],
                )
            )
        except Unauthenticated as exc:
            return _sign_in_failed(str(exc))
        response = RedirectResponse(
            _safe_next(pending.get("next")), status_code=status.HTTP_302_FOUND
        )
        ttl = int(config.session_hours * 3600)
        cookie(
            response,
            SESSION_COOKIE,
            signer.dumps(
                {**principal_to_session(principal), "it": id_token},
                purpose="session",
                ttl_seconds=ttl,
            ),
            ttl,
        )
        response.delete_cookie(LOGIN_COOKIE, path="/")
        return response

    @router.post("/logout")
    def logout(request: Request) -> JSONResponse:
        if request.headers.get("x-mlp-csrf") != "1":
            raise DomainHttpError(Unauthenticated("sign out from the UI"))
        session = signer.loads(request.cookies.get(SESSION_COOKIE, ""), purpose="session") or {}
        url = login.logout_url(
            post_logout_redirect_uri=f"{base_url(request)}/ui/", id_token_hint=session.get("it")
        )
        response = JSONResponse({"logout_url": url})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return router


def _sign_in_failed(reason: str) -> Response:
    # Plain text on purpose: the UI's CSP forbids inline content, and this page is a dead end
    # with one way out.
    return Response(
        f"Sign-in failed: {reason}\n\nStart again: /auth/login\n",
        status_code=400,
        media_type="text/plain",
    )
