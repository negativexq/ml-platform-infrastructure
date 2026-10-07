"""Immutable S3 connection and dataset identities; credentials stay in SecretRefs."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

from controlplane.domain.entities import validate_slug
from controlplane.domain.errors import InvalidArgument
from controlplane.domain.ids import new_id
from controlplane.domain.secrets import secret_name


class DatasetFormat(StrEnum):
    CSV = "CSV"
    PARQUET = "PARQUET"


@dataclass(frozen=True, kw_only=True)
class DataConnection:
    project_id: UUID
    name: str
    endpoint: str
    bucket: str
    credential_secret: str
    region: str = "us-east-1"
    prefix: str = ""
    id: UUID = field(default_factory=new_id)
    created_at: datetime

    def __post_init__(self) -> None:
        validate_slug(self.name, "connection name")
        if len(self.endpoint) > 512 or any(ord(c) <= 32 for c in self.endpoint):
            raise InvalidArgument("invalid S3 endpoint")
        secret_name(self.credential_secret)
        try:
            uri = urlsplit(self.endpoint)
            if (
                uri.scheme not in {"http", "https"}
                or not uri.hostname
                or uri.username
                or uri.password
                or uri.path not in {"", "/"}
                or uri.query
                or uri.fragment
                or not 1 <= (443 if uri.port is None else uri.port) <= 65535
            ):
                raise ValueError()
        except ValueError as exc:
            raise InvalidArgument(
                "S3 endpoint requires an HTTP(S) origin without credentials"
            ) from exc
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket):
            raise InvalidArgument("invalid S3 bucket")
        if not re.fullmatch(r"[a-z0-9-]{1,64}", self.region):
            raise InvalidArgument("invalid S3 region")
        if len(self.prefix) > 1024 or self.prefix.startswith("/") or "\x00" in self.prefix:
            raise InvalidArgument("invalid S3 prefix")


@dataclass(frozen=True, slots=True)
class DatasetColumn:
    name: str
    dtype: str
    nullable: bool = False

    def __post_init__(self) -> None:
        if not self.name or len(self.name) > 128 or "\x00" in self.name:
            raise InvalidArgument("dataset column name must be 1-128 characters")
        if self.dtype not in {"string", "integer", "number", "boolean"}:
            raise InvalidArgument("dataset dtype must be string, integer, number or boolean")


@dataclass(frozen=True, kw_only=True)
class DatasetVersion:
    project_id: UUID
    connection_id: UUID
    name: str
    version: int
    uri: str
    format: DatasetFormat
    columns: tuple[DatasetColumn, ...]
    checksum_sha256: str | None = None
    object_version_id: str | None = None
    producer_run_id: UUID | None = None
    producer_pipeline_run_id: UUID | None = None
    row_count: int | None = None
    id: UUID = field(default_factory=new_id)
    created_at: datetime

    def __post_init__(self) -> None:
        validate_slug(self.name, "dataset name")
        object.__setattr__(self, "columns", tuple(self.columns))
        try:
            object.__setattr__(self, "format", DatasetFormat(self.format))
        except ValueError as exc:
            raise InvalidArgument("dataset format must be CSV or PARQUET") from exc
        if self.version < 1:
            raise InvalidArgument("dataset version must be positive")
        if (
            not self.columns
            or len(self.columns) > 512
            or len({c.name for c in self.columns}) != len(self.columns)
        ):
            raise InvalidArgument("dataset needs 1-512 unique columns")
        if self.checksum_sha256 is not None and not re.fullmatch(
            r"[a-f0-9]{64}", self.checksum_sha256
        ):
            raise InvalidArgument("checksum_sha256 must be a lowercase SHA-256 digest")
        if self.object_version_id is not None and (
            not self.object_version_id
            or len(self.object_version_id) > 1024
            or self.object_version_id == "null"
        ):
            raise InvalidArgument("object_version_id must identify a versioned S3 object")
        if not self.checksum_sha256 and not self.object_version_id:
            raise InvalidArgument("dataset requires checksum_sha256 or an S3 object_version_id")
        if self.producer_run_id and self.producer_pipeline_run_id:
            raise InvalidArgument("dataset has at most one producer run")
        if self.row_count is not None and self.row_count < 0:
            raise InvalidArgument("row_count must be nonnegative")
        uri = urlsplit(self.uri)
        if (
            uri.scheme != "s3"
            or not uri.netloc
            or not uri.path.strip("/")
            or uri.query
            or uri.fragment
            or uri.username
            or uri.password
            or "\x00" in self.uri
            or len(self.uri) > 4096
        ):
            raise InvalidArgument(
                "dataset URI must identify one s3://bucket/object without credentials"
            )

    def validate_connection(self, connection: DataConnection) -> None:
        uri = urlsplit(self.uri)
        key = uri.path[1:]
        prefix = connection.prefix.rstrip("/")
        if (
            self.project_id != connection.project_id
            or self.connection_id != connection.id
            or uri.netloc != connection.bucket
            or (prefix and key != prefix and not key.startswith(prefix + "/"))
        ):
            raise InvalidArgument(
                "dataset must belong to its project's connection bucket and prefix"
            )
