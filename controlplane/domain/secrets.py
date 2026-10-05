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
    storage_secret: str | None = None

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
        if self.storage_secret:
            secret_name(self.storage_secret)
        for name in self.image_pull_secrets:
            secret_name(name)
        if len(set(self.image_pull_secrets)) != len(self.image_pull_secrets):
            raise InvalidArgument("duplicate image pull secret")
        object.__setattr__(self, "env", dict(self.env))

    def to_json(self) -> dict[str, Any]:
        return {
            "env": {k: {"name": v.name, "key": v.key} for k, v in self.env.items()},
            "image_pull_secrets": list(self.image_pull_secrets),
            **({"storage_secret": self.storage_secret} if self.storage_secret else {}),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> SecretRefs:
        raw = raw or {}
        return cls(
            env={k: SecretKeyRef(**v) for k, v in raw.get("env", {}).items()},
            image_pull_secrets=tuple(raw.get("image_pull_secrets", [])),
            storage_secret=raw.get("storage_secret"),
        )

    @property
    def names(self) -> set[str]:
        return (
            {r.name for r in self.env.values()}
            | set(self.image_pull_secrets)
            | ({self.storage_secret} if self.storage_secret else set())
        )


S3_ANNOTATIONS = {
    "serving.kserve.io/s3-endpoint",
    "serving.kserve.io/s3-region",
    "serving.kserve.io/s3-usehttps",
    "serving.kserve.io/s3-useanoncredential",
}


def validate_storage_annotations(annotations: Mapping[str, str]) -> None:
    if set(annotations) - S3_ANNOTATIONS:
        raise InvalidArgument("only supported S3 storage annotations are accepted")
    for key, value in annotations.items():
        if key.endswith("s3-endpoint"):
            if not re.fullmatch(r"[a-zA-Z0-9.-]{1,253}(?::[0-9]{1,5})?", value):
                raise InvalidArgument("S3 endpoint requires a hostname and optional port")
        elif key.endswith("s3-region"):
            if not re.fullmatch(r"[a-z0-9-]{1,64}", value):
                raise InvalidArgument("invalid S3 region")
        elif key.endswith("s3-usehttps") and value not in {"0", "1"}:
            raise InvalidArgument("S3 HTTPS flag must be 0 or 1")
        elif key.endswith("s3-useanoncredential") and value != "false":
            raise InvalidArgument("credential secrets cannot enable anonymous storage access")
