from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self, cast

import pytest

from controlplane.application.ports import UnitOfWork
from controlplane.application.projects import CreateProject, ProjectService
from controlplane.domain.errors import Conflict, InvalidArgument, NotFound
from controlplane.domain.states import ProjectStatus

Factory = Callable[[], UnitOfWork]


@pytest.fixture
def service(uow_factory: Factory, clock: Callable[[], Any]) -> ProjectService:
    return ProjectService(uow_factory, clock)


def _audit(uow_factory: Factory, **kw: Any) -> list[Any]:
    with uow_factory() as uow:
        return list(uow.audit.list(**kw))


def test_create_persists_and_audits(service: ProjectService, uow_factory: Factory) -> None:
    project, created = service.create(CreateProject(name="credit-risk", display_name="Credit Risk"))
    assert created
    assert project.status is ProjectStatus.PENDING
    assert service.get(project.id) == project
    events = _audit(uow_factory, project_id=project.id)
    assert [e.action for e in events] == ["project.created"]
    assert events[0].entity_id == project.id


def test_identical_request_is_idempotent(service: ProjectService, uow_factory: Factory) -> None:
    cmd = CreateProject(name="credit-risk", display_name="Credit Risk", description="d")
    first, created_first = service.create(cmd)
    second, created_second = service.create(cmd)
    assert (created_first, created_second) == (True, False)
    assert first.id == second.id
    assert len(service.list()) == 1
    assert len(_audit(uow_factory)) == 1  # no second audit event


def test_same_name_different_attributes_conflicts(service: ProjectService) -> None:
    service.create(CreateProject(name="credit-risk", description="a"))
    with pytest.raises(Conflict):
        service.create(CreateProject(name="credit-risk", description="b"))


def test_invalid_name_is_rejected_and_writes_nothing(
    service: ProjectService, uow_factory: Factory
) -> None:
    with pytest.raises(InvalidArgument):
        service.create(CreateProject(name="Not Valid"))
    assert service.list() == []
    assert _audit(uow_factory) == []


def test_get_unknown_project(service: ProjectService) -> None:
    from uuid import uuid4

    with pytest.raises(NotFound):
        service.get(uuid4())


def test_list_is_ordered_and_paginated(service: ProjectService) -> None:
    for name in ("alpha", "bravo", "charlie"):
        service.create(CreateProject(name=name))
    assert [p.name for p in service.list()] == ["alpha", "bravo", "charlie"]
    assert [p.name for p in service.list(limit=2)] == ["alpha", "bravo"]
    assert [p.name for p in service.list(limit=2, offset=2)] == ["charlie"]


class _Wrapped:
    """Delegates to a real unit of work, with hooks for fault injection."""

    def __init__(
        self,
        inner: UnitOfWork,
        *,
        hide_first_lookup: list[bool] | None = None,
        audit_fails: bool = False,
    ) -> None:
        self._inner = inner
        self._hide = hide_first_lookup
        self._audit_fails = audit_fails

    @property
    def projects(self) -> Any:
        outer = self

        class Proxy:
            def get_by_name(self, name: str) -> Any:
                # Hide an existing row once, so the insert collides exactly as it
                # would when two identical requests interleave.
                if outer._hide:
                    outer._hide.pop()
                    return None
                return outer._inner.projects.get_by_name(name)

            def __getattr__(self, item: str) -> Any:
                return getattr(outer._inner.projects, item)

        return Proxy()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)  # every repository we do not intercept

    @property
    def audit(self) -> Any:
        if self._audit_fails:
            raise_on_record = type(
                "FailingAudit",
                (),
                {"record": lambda _s, _e: (_ for _ in ()).throw(RuntimeError("audit store down"))},
            )
            return raise_on_record()
        return self._inner.audit

    def __enter__(self) -> Self:
        self._inner.__enter__()
        return self

    def __exit__(
        self, et: type[BaseException] | None, e: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._inner.__exit__(et, e, tb)

    def commit(self) -> None:
        self._inner.commit()


def test_losing_a_creation_race_replays_the_winner(
    uow_factory: Factory, clock: Callable[[], Any]
) -> None:
    ProjectService(uow_factory, clock).create(CreateProject(name="credit-risk"))
    hide = [True]
    racy = ProjectService(
        lambda: cast(UnitOfWork, _Wrapped(uow_factory(), hide_first_lookup=hide)), clock
    )
    project, created = racy.create(CreateProject(name="credit-risk"))
    assert not created and hide == []  # the unique constraint, not the lookup, caught it
    assert len(_audit(uow_factory)) == 1


def test_a_failed_audit_write_rolls_the_project_back(
    uow_factory: Factory, clock: Callable[[], Any]
) -> None:
    svc = ProjectService(lambda: cast(UnitOfWork, _Wrapped(uow_factory(), audit_fails=True)), clock)
    with pytest.raises(RuntimeError):
        svc.create(CreateProject(name="credit-risk"))
    assert ProjectService(uow_factory, clock).list() == []


def test_concurrent_identical_requests_create_one_project(
    uow_factory: Factory, clock: Callable[[], Any]
) -> None:
    svc = ProjectService(uow_factory, clock)
    barrier = threading.Barrier(4)
    results: list[tuple[Any, bool]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            results.append(svc.create(CreateProject(name="credit-risk")))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len({p.id for p, _ in results}) == 1
    assert sum(created for _, created in results) == 1
    assert len(_audit(uow_factory)) == 1
