from collections.abc import Sequence
from dataclasses import asdict
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from controlplane.domain.data import DataConnection, DatasetColumn, DatasetFormat, DatasetVersion
from controlplane.domain.errors import AlreadyExists
from controlplane.persistence.models import DataConnectionRow, DatasetVersionRow


def connection(row: DataConnectionRow) -> DataConnection:
    return DataConnection(
        id=row.id, project_id=row.project_id, name=row.name, created_at=row.created_at, **row.spec
    )


def dataset(row: DatasetVersionRow) -> DatasetVersion:
    spec = dict(row.spec)
    spec["format"] = DatasetFormat(spec["format"])
    spec["columns"] = tuple(DatasetColumn(**c) for c in spec["columns"])
    return DatasetVersion(
        id=row.id,
        project_id=row.project_id,
        connection_id=row.connection_id,
        name=row.name,
        version=row.version,
        producer_run_id=row.producer_run_id,
        producer_pipeline_run_id=row.producer_pipeline_run_id,
        created_at=row.created_at,
        **spec,
    )


def values(
    entity: DataConnection | DatasetVersion, row: type[DataConnectionRow] | type[DatasetVersionRow]
) -> dict[str, Any]:
    data = asdict(entity)
    columns = set(row.__table__.columns.keys()) - {"spec"}
    return {
        **{k: v for k, v in data.items() if k in columns},
        "spec": {k: v for k, v in data.items() if k not in columns},
    }


class SqlDataCatalog:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add_connection(self, entity: DataConnection) -> None:
        self.session.add(DataConnectionRow(**values(entity, DataConnectionRow)))
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise AlreadyExists("data connection", entity.name) from exc

    def connection(self, id: UUID) -> DataConnection | None:
        row = self.session.get(DataConnectionRow, id)
        return connection(row) if row else None

    def connection_by_name(self, project_id: UUID, name: str) -> DataConnection | None:
        row = self.session.scalar(
            select(DataConnectionRow).where(
                DataConnectionRow.project_id == project_id, DataConnectionRow.name == name
            )
        )
        return connection(row) if row else None

    def connections(
        self, project_id: UUID, limit: int = 100, offset: int = 0
    ) -> Sequence[DataConnection]:
        return [
            connection(row)
            for row in self.session.scalars(
                select(DataConnectionRow)
                .where(DataConnectionRow.project_id == project_id)
                .order_by(DataConnectionRow.created_at.desc(), DataConnectionRow.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ]

    def references_secret(self, project_id: UUID, name: str) -> bool:
        return (
            self.session.scalar(
                select(DataConnectionRow.id)
                .where(
                    DataConnectionRow.project_id == project_id,
                    DataConnectionRow.spec["credential_secret"].astext == name,
                )
                .limit(1)
            )
            is not None
        )

    def add_dataset(self, entity: DatasetVersion) -> None:
        self.session.add(DatasetVersionRow(**values(entity, DatasetVersionRow)))
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise AlreadyExists("dataset version", entity.name) from exc

    def dataset(self, id: UUID) -> DatasetVersion | None:
        row = self.session.get(DatasetVersionRow, id)
        return dataset(row) if row else None

    def dataset_version(
        self, project_id: UUID, name: str, version: int | None = None
    ) -> DatasetVersion | None:
        query = select(DatasetVersionRow).where(
            DatasetVersionRow.project_id == project_id, DatasetVersionRow.name == name
        )
        if version is not None:
            query = query.where(DatasetVersionRow.version == version)
        row = self.session.scalar(query.order_by(DatasetVersionRow.version.desc()).limit(1))
        return dataset(row) if row else None

    def datasets(
        self, project_id: UUID, name: str | None = None, limit: int = 100, offset: int = 0
    ) -> Sequence[DatasetVersion]:
        query = select(DatasetVersionRow).where(DatasetVersionRow.project_id == project_id)
        if name is not None:
            query = query.where(DatasetVersionRow.name == name)
        return [
            dataset(row)
            for row in self.session.scalars(
                query.order_by(DatasetVersionRow.created_at.desc(), DatasetVersionRow.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ]
