from dataclasses import asdict
from datetime import datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from controlplane.domain.errors import AlreadyExists, Conflict
from controlplane.domain.monitoring_automation import (
    DatasetPublishedEvent,
    MonitoringExecution,
    MonitoringRule,
)
from controlplane.persistence.models import (
    DatasetPublishedEventRow,
    MonitoringExecutionRow,
    MonitoringRuleRow,
)


def entity(row, kind):
    return (
        kind(**{field: getattr(row, field) for field in kind.__dataclass_fields__}) if row else None
    )


class SqlMonitoringAutomation:
    def __init__(self, session: Session):
        self.session = session

    def rule(self, project_id: UUID, id: UUID, *, lock: bool = False):
        query = select(MonitoringRuleRow).where(
            MonitoringRuleRow.project_id == project_id, MonitoringRuleRow.id == id
        )
        if lock:
            query = query.with_for_update()
        return entity(self.session.scalar(query), MonitoringRule)

    def rules(self, project_id: UUID):
        return [
            entity(row, MonitoringRule)
            for row in self.session.scalars(
                select(MonitoringRuleRow)
                .where(MonitoringRuleRow.project_id == project_id)
                .order_by(MonitoringRuleRow.created_at, MonitoringRuleRow.id)
            )
        ]

    def add_rule(self, rule: MonitoringRule):
        self.session.add(MonitoringRuleRow(**asdict(rule)))
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise AlreadyExists("monitoring rule", rule.name) from exc

    def update_rule(self, rule: MonitoringRule, expected_revision: int):
        result = self.session.execute(
            update(MonitoringRuleRow)
            .where(MonitoringRuleRow.id == rule.id, MonitoringRuleRow.revision == expected_revision)
            .values(enabled=rule.enabled, revision=rule.revision, updated_at=rule.updated_at)
        )
        if result.rowcount != 1:
            raise Conflict("monitoring rule changed")

    def next_event(self):
        row = self.session.scalar(
            select(DatasetPublishedEventRow)
            .where(DatasetPublishedEventRow.processed_at.is_(None))
            .order_by(DatasetPublishedEventRow.created_at, DatasetPublishedEventRow.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return entity(row, DatasetPublishedEvent)

    def finish_event(self, id: UUID, now: datetime):
        self.session.execute(
            update(DatasetPublishedEventRow)
            .where(DatasetPublishedEventRow.id == id)
            .values(processed_at=now)
        )

    def add_execution(self, execution: MonitoringExecution):
        self.session.add(MonitoringExecutionRow(**asdict(execution)))
        self.session.flush()

    def update_execution(self, execution: MonitoringExecution):
        self.session.execute(
            update(MonitoringExecutionRow)
            .where(MonitoringExecutionRow.id == execution.id)
            .values(**asdict(execution))
        )

    def execution(self, rule_id: UUID, dataset_id: UUID):
        return entity(
            self.session.scalar(
                select(MonitoringExecutionRow).where(
                    MonitoringExecutionRow.rule_id == rule_id,
                    MonitoringExecutionRow.observed_dataset_id == dataset_id,
                )
            ),
            MonitoringExecution,
        )

    def executions(self, project_id: UUID, rule_id: UUID, limit: int, offset: int):
        return [
            entity(row, MonitoringExecution)
            for row in self.session.scalars(
                select(MonitoringExecutionRow)
                .where(
                    MonitoringExecutionRow.project_id == project_id,
                    MonitoringExecutionRow.rule_id == rule_id,
                )
                .order_by(
                    MonitoringExecutionRow.created_at.desc(), MonitoringExecutionRow.id.desc()
                )
                .limit(limit)
                .offset(offset)
            )
        ]

    def next_waiting(self, now: datetime):
        row = self.session.scalar(
            select(MonitoringExecutionRow)
            .where(
                MonitoringExecutionRow.status == "WAITING_FEEDBACK",
                MonitoringExecutionRow.next_attempt_at <= now,
            )
            .order_by(MonitoringExecutionRow.next_attempt_at, MonitoringExecutionRow.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        return entity(row, MonitoringExecution)
