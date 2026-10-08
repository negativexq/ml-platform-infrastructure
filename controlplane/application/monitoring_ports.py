from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from controlplane.domain.model_monitoring import MonitoringReport


class MonitoringRepository(Protocol):
    def add(self, report: MonitoringReport) -> None: ...

    def get(self, id: UUID) -> MonitoringReport | None: ...

    def list(
        self,
        project_id: UUID,
        *,
        model_version_id: UUID | None = None,
        job_run_id: UUID | None = None,
        pipeline_run_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[MonitoringReport]: ...
