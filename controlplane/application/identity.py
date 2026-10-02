"""The caller, for the duration of one request, and the checks made against them.

The API sets the principal for each request (`bind_principal`); services read the actor for
their audit events from here, so who did something is never something a client asserts in a
request body. Outside a request (reconcilers, tests, a server running without authentication)
the actor is `anonymous` unless something else is bound.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextvars import ContextVar, Token
from typing import Protocol
from uuid import UUID

from controlplane.application.ports import UnitOfWork
from controlplane.domain.access import Principal, ProjectRole, highest
from controlplane.domain.errors import PermissionDenied

ANONYMOUS = "anonymous"

_principal: ContextVar[Principal | None] = ContextVar("mlp_principal", default=None)


def bind_principal(principal: Principal | None) -> Token[Principal | None]:
    return _principal.set(principal)


def reset_principal(token: Token[Principal | None]) -> None:
    _principal.reset(token)


def current_principal() -> Principal | None:
    return _principal.get()


def current_actor() -> str:
    """What audit events record as the actor of a change made right now."""
    principal = _principal.get()
    return principal.username if principal is not None else ANONYMOUS


class Authenticator(Protocol):
    """Turns a bearer token into a principal, or raises Unauthenticated."""

    def authenticate(self, token: str) -> Principal: ...


def role_in(uow: UnitOfWork, project_id: UUID, principal: Principal) -> ProjectRole | None:
    """The principal's effective role in a project (platform admins are admins everywhere)."""
    if principal.platform_admin:
        return ProjectRole.ADMIN
    memberships = uow.memberships.list_for_subjects(principal.subjects)
    return highest(m.role for m in memberships if m.project_id == project_id)


def require(uow: UnitOfWork, project_id: UUID, principal: Principal, needed: ProjectRole) -> None:
    role = role_in(uow, project_id, principal)
    if role is None or not role.includes(needed):
        have = f"your role here is {role.value}" if role else "you are not a member of this project"
        raise PermissionDenied(f"this needs the {needed.value} role in the project; {have}")


def visible_project_ids(uow: UnitOfWork, principal: Principal) -> Sequence[UUID] | None:
    """Projects the principal is a member of; None means all of them (platform admin)."""
    if principal.platform_admin:
        return None
    return sorted({m.project_id for m in uow.memberships.list_for_subjects(principal.subjects)})


class LoginProvider(Protocol):
    """Browser sign-in (OpenID Connect authorization code flow with PKCE), run server-side so
    tokens never reach the page."""

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str: ...

    def complete(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> tuple[Principal, str | None]:
        """Exchange the code, verify the ID token, and say who signed in. Also returns the
        ID token itself, kept only to end the provider's session at sign-out."""

    def logout_url(
        self, *, post_logout_redirect_uri: str, id_token_hint: str | None = None
    ) -> str | None: ...
