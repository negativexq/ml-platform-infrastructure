from collections.abc import Sequence
from uuid import UUID

from controlplane.domain.data import DataConnection, DatasetVersion
from controlplane.domain.errors import AlreadyExists


class MemoryDataCatalog:
    def __init__(
        self, connections: dict[UUID, DataConnection], datasets: dict[UUID, DatasetVersion]
    ) -> None:
        self._connections, self._datasets = connections, datasets

    def add_connection(self, entity: DataConnection) -> None:
        if self.connection_by_name(entity.project_id, entity.name):
            raise AlreadyExists("data connection", entity.name)
        self._connections[entity.id] = entity

    def connection(self, id: UUID) -> DataConnection | None:
        return self._connections.get(id)

    def connection_by_name(self, project_id: UUID, name: str) -> DataConnection | None:
        return next(
            (
                e
                for e in self._connections.values()
                if e.project_id == project_id and e.name == name
            ),
            None,
        )

    def connections(
        self, project_id: UUID, limit: int = 100, offset: int = 0
    ) -> Sequence[DataConnection]:
        values = sorted(
            (e for e in self._connections.values() if e.project_id == project_id),
            key=lambda e: (e.created_at, e.id),
            reverse=True,
        )
        return values[offset : offset + limit]

    def references_secret(self, project_id: UUID, name: str) -> bool:
        return any(
            e.project_id == project_id and e.credential_secret == name
            for e in self._connections.values()
        )

    def add_dataset(self, entity: DatasetVersion) -> None:
        if self.dataset_version(entity.project_id, entity.name, entity.version):
            raise AlreadyExists("dataset version", entity.name)
        self._datasets[entity.id] = entity

    def dataset(self, id: UUID) -> DatasetVersion | None:
        return self._datasets.get(id)

    def dataset_version(
        self, project_id: UUID, name: str, version: int | None = None
    ) -> DatasetVersion | None:
        values = [
            e
            for e in self._datasets.values()
            if e.project_id == project_id
            and e.name == name
            and (version is None or version == e.version)
        ]
        return max(values, key=lambda e: e.version, default=None)

    def datasets(
        self, project_id: UUID, name: str | None = None, limit: int = 100, offset: int = 0
    ) -> Sequence[DatasetVersion]:
        values = sorted(
            (
                e
                for e in self._datasets.values()
                if e.project_id == project_id and (name is None or e.name == name)
            ),
            key=lambda e: (e.created_at, e.id),
            reverse=True,
        )
        return values[offset : offset + limit]
