from collections.abc import Sequence
from uuid import UUID

from controlplane.domain.errors import AlreadyExists
from controlplane.domain.model_monitoring import MonitoringReport


class MemoryMonitoring:
    def __init__(self, data: dict[UUID, MonitoringReport]) -> None:
        self.data = data

    def add(self, report: MonitoringReport) -> None:
        if report.id in self.data or any(
            (r.job_run_id, r.pipeline_run_id, r.step)
            == (report.job_run_id, report.pipeline_run_id, report.step)
            for r in self.data.values()
        ):
            raise AlreadyExists("monitoring report", report.id)
        self.data[report.id] = report

    def get(self, id: UUID) -> MonitoringReport | None:
        return self.data.get(id)

    def list(
        self,
        project_id: UUID,
        *,
        model_version_id: UUID | None = None,
        job_run_id: UUID | None = None,
        pipeline_run_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[MonitoringReport]:
        return sorted(
            (
                r
                for r in self.data.values()
                if r.project_id == project_id
                and (model_version_id is None or r.model_version_id == model_version_id)
                and (job_run_id is None or r.job_run_id == job_run_id)
                and (pipeline_run_id is None or r.pipeline_run_id == pipeline_run_id)
            ),
            key=lambda r: (r.created_at, r.id),
            reverse=True,
        )[offset : offset + limit]
