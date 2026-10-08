from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from controlplane.domain.data import DataConnection, DatasetVersion


class DataCatalogRepository(Protocol):
    def add_connection(self, entity: DataConnection) -> None: ...
    def connection(self, id: UUID) -> DataConnection | None: ...
    def connection_by_name(self, project_id: UUID, name: str) -> DataConnection | None: ...
    def connections(
        self, project_id: UUID, limit: int = 100, offset: int = 0
    ) -> Sequence[DataConnection]: ...
    def references_secret(self, project_id: UUID, name: str) -> bool: ...

    def add_dataset(self, entity: DatasetVersion) -> None: ...
    def dataset(self, id: UUID) -> DatasetVersion | None: ...
    def dataset_version(
        self, project_id: UUID, name: str, version: int | None = None
    ) -> DatasetVersion | None: ...
    def datasets(
        self,
        project_id: UUID,
        name: str | None = None,
        limit: int = 100,
        offset: int = 0,
        producer_run_id: UUID | None = None,
        producer_pipeline_run_id: UUID | None = None,
    ) -> Sequence[DatasetVersion]: ...
