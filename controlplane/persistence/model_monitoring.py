from collections.abc import Sequence
from dataclasses import asdict, fields
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from controlplane.domain.errors import AlreadyExists
from controlplane.domain.model_monitoring import MonitoringReport
from controlplane.persistence.models import ModelVersionRow, MonitoringReportRow


class SqlMonitoring:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, report: MonitoringReport) -> None:
        model_id = self.session.scalar(
            select(ModelVersionRow.model_id).where(ModelVersionRow.id == report.model_version_id)
        )
        self.session.add(MonitoringReportRow(model_id=model_id, **asdict(report)))
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise AlreadyExists("monitoring report", report.id) from exc

    @staticmethod
    def entity(row: MonitoringReportRow) -> MonitoringReport:
        return MonitoringReport(
            **{field.name: getattr(row, field.name) for field in fields(MonitoringReport)}
        )

    def get(self, id: UUID) -> MonitoringReport | None:
        row = self.session.get(MonitoringReportRow, id)
        return self.entity(row) if row else None

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
        query = select(MonitoringReportRow).where(MonitoringReportRow.project_id == project_id)
        if model_version_id:
            query = query.where(MonitoringReportRow.model_version_id == model_version_id)
        if job_run_id is not None:
            query = query.where(MonitoringReportRow.job_run_id == job_run_id)
        if pipeline_run_id is not None:
            query = query.where(MonitoringReportRow.pipeline_run_id == pipeline_run_id)
        return [
            self.entity(row)
            for row in self.session.scalars(
                query.order_by(MonitoringReportRow.created_at.desc(), MonitoringReportRow.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ]
