"""Names and references only. Secret values never belong to platform entities."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from controlplane.domain.errors import InvalidArgument

_NAME_PART = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_KEY = re.compile(r"^[A-Za-z0-9._-]{1,253}$")
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def secret_name(value: str) -> str:
    if len(value) > 253 or any(not _NAME_PART.fullmatch(part) for part in value.split(".")):
        raise InvalidArgument("invalid secret name")
    return value


def secret_key(value: str) -> str:
    if not _KEY.fullmatch(value):
        raise InvalidArgument("invalid secret key")
    return value


@dataclass(frozen=True, slots=True)
class SecretKeyRef:
    name: str
    key: str

    def __post_init__(self) -> None:
        secret_name(self.name)
        secret_key(self.key)


@dataclass(frozen=True, slots=True)
class SecretRefs:
    env: Mapping[str, SecretKeyRef] = field(default_factory=dict)
    image_pull_secrets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.env) > 64 or len(self.image_pull_secrets) > 16:
            raise InvalidArgument("too many secret references")
        for key in self.env:
            if (
                not _ENV.fullmatch(key)
                or key.startswith("MLP_")
                or key in {"MLFLOW_TRACKING_URI", "MLFLOW_EXPERIMENT_NAME"}
            ):
                raise InvalidArgument("invalid or platform-owned secret environment name")
        for name in self.image_pull_secrets:
            secret_name(name)
        if len(set(self.image_pull_secrets)) != len(self.image_pull_secrets):
            raise InvalidArgument("duplicate image pull secret")
        object.__setattr__(self, "env", dict(self.env))

    def to_json(self) -> dict[str, Any]:
        return {
            "env": {k: {"name": v.name, "key": v.key} for k, v in self.env.items()},
            "image_pull_secrets": list(self.image_pull_secrets),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> SecretRefs:
        raw = raw or {}
        return cls(
            env={k: SecretKeyRef(**v) for k, v in raw.get("env", {}).items()},
            image_pull_secrets=tuple(raw.get("image_pull_secrets", [])),
        )

    @property
    def names(self) -> set[str]:
        return {r.name for r in self.env.values()} | set(self.image_pull_secrets)
