"""Keeps the registry's `champion` / `candidate` aliases equal to platform state.

Platform state is the truth; the registry is brought in line with it, never the
reverse. An alias that points somewhere else than the platform says is DRIFT:
it is recorded (flag + audit event), repaired, and the flag is cleared only after
a re-read confirms the repair.

A missing alias is treated as "not synced yet" (the normal state right after a
promotion), not as drift, because the two cannot be told apart without history.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from controlplane.application.models import ALIAS_CANDIDATE, ALIAS_CHAMPION
from controlplane.application.projects import Clock, UnitOfWorkFactory, utc_now
from controlplane.application.providers import ExperimentProvider
from controlplane.domain.audit import AuditEvent
from controlplane.domain.entities import Model, ModelVersion
from controlplane.domain.states import ModelKind, ModelStatus
from controlplane.reconciliation.watchdog import heartbeat

SYSTEM = "reconciler"


@dataclass(frozen=True, slots=True)
class AliasResult:
    model_id: UUID
    synced: tuple[str, ...] = ()  # aliases that had to be written or removed
    drift: str | None = None  # still drifted after this pass (repair failed)
    drift_detected: bool = False


def desired_aliases(versions: list[ModelVersion]) -> dict[str, str | None]:
    champion = next((v for v in versions if v.status is ModelStatus.CHAMPION), None)
    candidates = [v for v in versions if v.status is ModelStatus.CANDIDATE]
    newest = max(candidates, key=lambda v: v.version, default=None)
    return {
        ALIAS_CHAMPION: champion.external_ref if champion else None,
        ALIAS_CANDIDATE: newest.external_ref if newest else None,
    }


class ModelAliasReconciler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        experiments: ExperimentProvider,
        clock: Clock = utc_now,
    ) -> None:
        self._uow_factory = uow_factory
        self._experiments = experiments
        self._clock = clock

    def reconcile_all(self) -> list[AliasResult]:
        with self._uow_factory() as uow:
            ids = [m.id for m in uow.models.list_all() if m.kind is ModelKind.CLASSIC]
        results = []
        for model_id in ids:
            heartbeat()
            try:
                results.append(self.reconcile(model_id))
            except Exception as exc:  # noqa: BLE001 - registry trouble must not stop other models
                self._set_drift(model_id, f"alias sync failed: {type(exc).__name__}: {exc}")
        return results

    def reconcile(self, model_id: UUID) -> AliasResult:
        with self._uow_factory() as uow:
            model = uow.models.get(model_id)
            if model is None or model.kind is not ModelKind.CLASSIC:
                return AliasResult(model_id)
            project = uow.projects.get(model.project_id)
            versions = list(uow.model_versions.list(model.id))
        if project is None or not versions:
            return AliasResult(model_id)

        registry = model.registry_name(project.name)
        synced: list[str] = []
        drifted: list[str] = []
        for alias, wanted in desired_aliases(versions).items():
            actual = self._experiments.get_model_alias(registry, alias)
            if actual == wanted:
                continue
            if actual is not None:  # someone moved it, or it outlived its version
                drifted.append(f"{alias}: platform says {wanted or 'none'}, registry has {actual}")
            if wanted is None:
                self._experiments.delete_model_alias(registry, alias)
            else:
                self._experiments.set_model_alias(registry, alias, wanted)
            if self._experiments.get_model_alias(registry, alias) != wanted:
                raise RuntimeError(f"alias {alias!r} did not converge")
            synced.append(alias)

        if drifted:
            self._record(
                model, "model.alias_drift_detected", "; ".join(drifted), drift="; ".join(drifted)
            )
        if synced:
            self._record(model, "model.alias_synced", None, aliases=synced, drift=None)
        elif model.alias_drift is not None:
            self._set_drift(model_id, None)
        return AliasResult(model_id, tuple(synced), None, bool(drifted))

    def _record(
        self,
        model: Model,
        action: str,
        reason: str | None,
        *,
        drift: str | None = None,
        aliases: list[str] | None = None,
    ) -> None:
        now = self._clock()
        with self._uow_factory() as uow:
            current = uow.models.get(model.id)
            if current is None:
                return
            # While drift is being repaired the flag is set; once repaired it is cleared.
            uow.models.update_alias_drift(current.with_alias_drift(drift))
            uow.audit.record(
                AuditEvent(
                    occurred_at=now,
                    actor=SYSTEM,
                    action=action,
                    entity_type="model",
                    entity_id=model.id,
                    project_id=model.project_id,
                    payload={"reason": reason, "aliases": aliases or []},
                )
            )
            uow.commit()

    def _set_drift(self, model_id: UUID, drift: str | None) -> None:
        with self._uow_factory() as uow:
            model = uow.models.get(model_id)
            if model is None or model.alias_drift == drift:
                return
            uow.models.update_alias_drift(model.with_alias_drift(drift))
            uow.commit()
