"""Models, their versions, evaluation and promotion.

Platform state lives in the database. The registry (MLflow) supplies artifacts and
version numbers; it never decides what a version's status is, and nothing is
imported from it blindly: a version enters the platform only through `discover`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from controlplane.application.jobs import resolve_project
from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import ANONYMOUS, Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import ExperimentProvider
from controlplane.application.workflow_compiler import TAG_PIPELINE_RUN_ID
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import (
    Evaluation,
    Model,
    ModelVersion,
    Project,
    Promotion,
    Threshold,
    run_checks,
)
from controlplane.domain.errors import AlreadyExists, Conflict, NotFound
from controlplane.domain.states import EvaluationStatus, ModelStatus, PromotionStatus

ALIAS_CHAMPION = "champion"
ALIAS_CANDIDATE = "candidate"


def _audit(
    now: datetime,
    action: str,
    entity_type: str,
    entity_id: UUID,
    project_id: UUID,
    **payload: object,
) -> AuditEvent:
    return AuditEvent(
        occurred_at=now,
        actor=ANONYMOUS,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        project_id=project_id,
        payload=payload,
    )


@dataclass(frozen=True, slots=True)
class ModelView:
    model: Model
    registry_name: str
    versions: Sequence[ModelVersion]

    @property
    def champion(self) -> ModelVersion | None:
        return next((v for v in self.versions if v.status is ModelStatus.CHAMPION), None)


@dataclass(frozen=True, slots=True)
class VersionView:
    version: ModelVersion
    model: Model
    evaluations: Sequence[Evaluation]
    promotions: Sequence[Promotion]


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    created: Sequence[ModelVersion]
    existing: int


class ModelService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock = utc_now,
        experiments: ExperimentProvider | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._experiments = experiments

    # -- models -------------------------------------------------------------

    def create(
        self, project_ref: str, name: str, thresholds: Mapping[str, Threshold]
    ) -> tuple[ModelView, bool]:
        """Identical repeat -> existing, `created=False`. Same name with different
        thresholds -> Conflict: change thresholds explicitly with `set_thresholds`."""
        try:
            return self._create(project_ref, name, thresholds)
        except AlreadyExists:  # lost a race with an identical request
            return self._create(project_ref, name, thresholds)

    def _create(
        self, project_ref: str, name: str, thresholds: Mapping[str, Threshold]
    ) -> tuple[ModelView, bool]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            candidate = Model.create(
                project_id=project.id, name=name, thresholds=thresholds, now=self._clock()
            )
            existing = uow.models.get_by_name(project.id, candidate.name)
            if existing is not None:
                if dict(existing.thresholds) != dict(candidate.thresholds):
                    raise Conflict(
                        f"model {name!r} already exists with different thresholds; "
                        "use PUT .../thresholds to change them"
                    )
                return self._view(uow, project, existing), False
            uow.models.add(candidate)
            uow.audit.record(
                _audit(
                    candidate.created_at,
                    "model.created",
                    "model",
                    candidate.id,
                    project.id,
                    name=candidate.name,
                )
            )
            uow.commit()
            return self._view(uow, project, candidate), True

    def set_thresholds(
        self, project_ref: str, name: str, thresholds: Mapping[str, Threshold]
    ) -> ModelView:
        """Applies to evaluations started afterwards; recorded evaluations keep the
        thresholds they were judged against."""
        with self._uow_factory() as uow:
            project, model = self._load(uow, project_ref, name)
            updated = model.with_thresholds(thresholds)
            uow.models.update(updated)
            uow.audit.record(
                _audit(
                    self._clock(),
                    "model.thresholds_changed",
                    "model",
                    model.id,
                    project.id,
                    metrics=sorted(thresholds),
                )
            )
            uow.commit()
            return self._view(uow, project, updated)

    def get(self, project_ref: str, name: str) -> ModelView:
        with self._uow_factory() as uow:
            project, model = self._load(uow, project_ref, name)
            return self._view(uow, project, model)

    def list(self, project_ref: str) -> Sequence[ModelView]:
        with self._uow_factory() as uow:
            project = resolve_project(uow, project_ref)
            return [self._view(uow, project, m) for m in uow.models.list(project.id)]

    # -- versions -----------------------------------------------------------

    def discover(self, project_ref: str, name: str) -> DiscoveryResult:
        """Register the registry's versions the platform has not seen yet.
        Idempotent: a version already known (by registry reference) is never duplicated."""
        if self._experiments is None:
            raise Conflict("no model registry is configured")
        with self._uow_factory() as uow:
            project, model = self._load(uow, project_ref, name)
        registry = model.registry_name(project.name)
        found = self._experiments.list_model_versions(registry)

        created: list[ModelVersion] = []
        existing = 0
        for registered in found:
            lineage = self._lineage(registered.run_ref)
            for _ in range(3):  # a concurrent discover can take our version number
                try:
                    version, was_new = self._register(model, project, registered.ref, lineage)
                    break
                except AlreadyExists:
                    continue
            else:
                raise AlreadyExists("model version", registered.ref)
            if was_new:
                created.append(version)
            else:
                existing += 1
        return DiscoveryResult(created, existing)

    def _lineage(self, run_ref: str | None) -> UUID | None:
        """The pipeline run that produced a registry version, from the tracking run's tags."""
        if run_ref is None or self._experiments is None:
            return None
        try:
            raw = self._experiments.get_run(run_ref).tags.get(TAG_PIPELINE_RUN_ID)
            return UUID(raw) if raw else None
        except (NotFound, ValueError):
            return None

    def _register(
        self, model: Model, project: Project, ref: str, lineage: UUID | None
    ) -> tuple[ModelVersion, bool]:
        with self._uow_factory() as uow:
            known = uow.model_versions.get_by_ref(model.id, ref)
            if known is not None:
                return known, False
            now = self._clock()
            version = ModelVersion(
                model_id=model.id,
                version=uow.model_versions.next_version(model.id),
                external_ref=ref,
                source_pipeline_run_id=lineage,
                created_at=now,
                updated_at=now,
            )
            uow.model_versions.add(version)
            uow.audit.record(
                _audit(
                    now,
                    "model_version.registered",
                    "model_version",
                    version.id,
                    project.id,
                    model=model.name,
                    version=version.version,
                )
            )
            uow.commit()
            return version, True

    def get_version(self, version_id: UUID) -> VersionView:
        with self._uow_factory() as uow:
            return self._version_view(uow, version_id)

    @staticmethod
    def _version_view(uow: UnitOfWork, version_id: UUID) -> VersionView:
        version = uow.model_versions.get(version_id)
        if version is None:
            raise NotFound("model version", version_id)
        model = uow.models.get(version.model_id)
        assert model is not None
        return VersionView(
            version,
            model,
            uow.evaluations.list_for_version(version.id),
            uow.promotions.list_for_versions([version.id]),
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _load(uow: UnitOfWork, project_ref: str, name: str) -> tuple[Project, Model]:
        project = resolve_project(uow, project_ref)
        model = uow.models.get_by_name(project.id, name)
        if model is None:
            raise NotFound("model", name)
        return project, model

    @staticmethod
    def _view(uow: UnitOfWork, project: Project, model: Model) -> ModelView:
        return ModelView(
            model, model.registry_name(project.name), uow.model_versions.list(model.id)
        )


class EvaluationService:
    """Judges a version against its model's thresholds. REGISTERED -> EVALUATING, then
    CANDIDATE (all checks pass) or REJECTED. Reading the metrics can fail for
    infrastructure reasons; that leaves the version EVALUATING so it can be retried,
    never REJECTED."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        experiments: ExperimentProvider,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._experiments = experiments
        self._clock = clock

    def evaluate(self, version_id: UUID) -> VersionView:
        model, project, version, evaluation = self._start(version_id)
        metrics = self._read_metrics(model.registry_name(project.name), version)
        return self._finish(model, project, version, evaluation, metrics)

    def _start(self, version_id: UUID) -> tuple[Model, Project, ModelVersion, Evaluation]:
        with self._uow_factory() as uow:
            version = uow.model_versions.get(version_id)
            if version is None:
                raise NotFound("model version", version_id)
            model = uow.models.get(version.model_id)
            assert model is not None
            project = uow.projects.get(model.project_id)
            assert project is not None
            if version.status not in (ModelStatus.REGISTERED, ModelStatus.EVALUATING):
                raise Conflict(
                    f"version {version.version} is {version.status.value}; only REGISTERED "
                    "or EVALUATING versions can be evaluated"
                )
            if not model.thresholds:
                raise Conflict(
                    f"model {model.name!r} has no thresholds; nothing to evaluate against"
                )
            now = self._clock()
            if version.status is ModelStatus.REGISTERED:
                version = version.transition_to(ModelStatus.EVALUATING, now)
                uow.model_versions.update(version, expected_status=ModelStatus.REGISTERED)
            running = next(
                (
                    e
                    for e in uow.evaluations.list_for_version(version.id)
                    if e.status is EvaluationStatus.RUNNING
                ),
                None,
            )
            if running is None:
                champion = uow.model_versions.get_champion(model.id)
                pending = Evaluation(
                    model_version_id=version.id,
                    baseline_version_id=champion.id if champion else None,
                    created_at=now,
                    updated_at=now,
                )
                uow.evaluations.add(pending)
                running = pending.transition_to(EvaluationStatus.RUNNING, now)
                uow.evaluations.update(running, expected_status=EvaluationStatus.PENDING)
            uow.audit.record(
                _audit(
                    now,
                    "evaluation.started",
                    "evaluation",
                    running.id,
                    project.id,
                    version=version.version,
                )
            )
            uow.commit()
            return model, project, version, running

    def _read_metrics(self, registry: str, version: ModelVersion) -> Mapping[str, float]:
        run_ref = next(
            (
                v.run_ref
                for v in self._experiments.list_model_versions(registry)
                if v.ref == version.external_ref
            ),
            None,
        )
        if run_ref is None:
            return {}  # no tracking run behind this version: every threshold will fail
        return self._experiments.get_run(run_ref).metrics

    def _finish(
        self,
        model: Model,
        project: Project,
        version: ModelVersion,
        evaluation: Evaluation,
        metrics: Mapping[str, float],
    ) -> VersionView:
        checks = run_checks(metrics, model.thresholds)
        passed = all(c.passed for c in checks)
        with self._uow_factory() as uow:
            now = self._clock()
            done = evaluation.with_result(metrics, checks, now).transition_to(
                EvaluationStatus.PASSED if passed else EvaluationStatus.FAILED, now
            )
            uow.evaluations.update(done, expected_status=EvaluationStatus.RUNNING)
            outcome = version.transition_to(
                ModelStatus.CANDIDATE if passed else ModelStatus.REJECTED, now
            )
            uow.model_versions.update(outcome, expected_status=ModelStatus.EVALUATING)
            uow.audit.record(
                _audit(
                    now,
                    f"evaluation.{done.status.value.lower()}",
                    "evaluation",
                    done.id,
                    project.id,
                    version=version.version,
                    failed=[c.metric for c in checks if not c.passed],
                )
            )
            uow.audit.record(
                _audit(
                    now,
                    f"model_version.{outcome.status.value.lower()}",
                    "model_version",
                    version.id,
                    project.id,
                    model=model.name,
                    version=version.version,
                )
            )
            uow.commit()
            return ModelService._version_view(uow, version.id)


class PromotionService:
    """CANDIDATE -> CHAMPION, atomically with archiving the previous champion and the
    audit record. The registry alias is brought in line afterwards by the
    ModelAliasReconciler; the database commit is the promotion."""

    def __init__(self, uow_factory: UnitOfWorkFactory, clock: Clock = utc_now) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def promote(self, version_id: UUID) -> VersionView:
        with self._uow_factory() as uow:
            version = uow.model_versions.get(version_id)
            if version is None:
                raise NotFound("model version", version_id)
            model = uow.models.get(version.model_id)
            assert model is not None
            if version.status is ModelStatus.CHAMPION:
                return ModelService._version_view(uow, version.id)  # idempotent
            if version.status is ModelStatus.REJECTED:
                raise Conflict(
                    f"version {version.version} was rejected by evaluation and cannot be promoted"
                )
            if version.status is not ModelStatus.CANDIDATE:
                raise Conflict(
                    f"version {version.version} is {version.status.value}; "
                    "only a CANDIDATE can be promoted"
                )
            now = self._clock()
            previous = uow.model_versions.get_champion(model.id)
            if previous is not None:
                uow.model_versions.update(
                    previous.transition_to(ModelStatus.ARCHIVED, now),
                    expected_status=ModelStatus.CHAMPION,
                )
            uow.model_versions.update(
                version.transition_to(ModelStatus.CHAMPION, now),
                expected_status=ModelStatus.CANDIDATE,
            )
            promotion = Promotion(
                model_version_id=version.id,
                previous_champion_id=previous.id if previous else None,
                created_at=now,
                updated_at=now,
            ).transition_to(PromotionStatus.APPLIED, now)
            uow.promotions.add(promotion)
            uow.audit.record(
                _audit(
                    now,
                    "model_version.promoted",
                    "model_version",
                    version.id,
                    model.project_id,
                    model=model.name,
                    version=version.version,
                    previous_champion=str(previous.id) if previous else None,
                    status=PromotionStatus.APPLIED.value,
                )
            )
            uow.commit()
            return ModelService._version_view(uow, version.id)
