"""Who may do what in a project.

Three roles, each including the ones below it:

    viewer    read everything in the project
    operator  start and cancel runs, register and evaluate models, deploy, start canaries
    admin     everything, plus deleting the project, changing acceptance thresholds and
              deciding who is a member

A member is an OIDC user (`user:oidc-<digest>`; local dev `user:alice`)
or a group from the identity provider (`group:ml-team`).
Someone's role in a project is the highest role among their own and their groups'.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from controlplane.domain.errors import InvalidArgument
from controlplane.domain.ids import new_id


class ProjectRole(StrEnum):
    INVOKER = "invoker"  # may only call the project's public endpoints through the gateway
    VIEWER = "viewer"
    OPERATOR = "operator"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _RANK[self]

    def includes(self, other: ProjectRole) -> bool:
        return self.rank >= other.rank


_RANK = {
    ProjectRole.INVOKER: 0,
    ProjectRole.VIEWER: 1,
    ProjectRole.OPERATOR: 2,
    ProjectRole.ADMIN: 3,
}

# `user:<stable-id>` or `group:<name>`. OIDC user IDs are bounded issuer/sub digests;
# explicitly configured static/development identities may still use a local username.
_SUBJECT = re.compile(r"^(user|group):[^\s:][^\s]{0,199}$")


def validate_subject(value: str) -> str:
    if not _SUBJECT.match(value):
        raise InvalidArgument(
            f"a member is 'user:<name>' or 'group:<name>' (no spaces): got {value!r}"
        )
    return value


def highest(roles: Iterable[ProjectRole]) -> ProjectRole | None:
    return max(roles, key=lambda r: r.rank, default=None)


@dataclass(frozen=True, slots=True, kw_only=True)
class Membership:
    id: UUID = field(default_factory=new_id)
    project_id: UUID
    subject: str
    role: ProjectRole
    created_at: datetime
    updated_at: datetime

    @classmethod
    def create(cls, *, project_id: UUID, subject: str, role: ProjectRole, now: datetime) -> Self:
        return cls(
            project_id=project_id,
            subject=validate_subject(subject),
            role=ProjectRole(role),
            created_at=now,
            updated_at=now,
        )


@dataclass(frozen=True, slots=True)
class Principal:
    """The authenticated caller. `subjects` are what memberships are matched against."""

    username: str
    groups: tuple[str, ...] = ()
    email: str | None = None
    display_name: str | None = None
    platform_admin: bool = False  # sees and may do everything, in every project

    subject_id: str | None = None  # stable issuer-scoped OIDC identity; None for static/dev callers

    @property
    def user_subject(self) -> str:
        return f"user:{self.subject_id or self.username}"

    @property
    def subjects(self) -> tuple[str, ...]:
        return (self.user_subject, *(f"group:{g}" for g in self.groups))
