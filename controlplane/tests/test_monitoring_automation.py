import json
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from controlplane.api.app import create_app
from controlplane.application.monitoring_automation import (
    MonitoringAutomationService,
    MonitoringDispatcher,
)
from controlplane.domain.errors import Conflict
from controlplane.tests.test_batch_inference import IMAGE, batch  # noqa: F401
from controlplane.tests.test_data_catalog import env, spec  # noqa: F401


@pytest.fixture
def automation(request, uow_factory, clock):
    _, fields, catalog, provider = request.getfixturevalue("batch")
    with uow_factory() as uow:
        observed = uow.data_catalog.dataset(fields["input_dataset_id"])
        connection = uow.data_catalog.connection(observed.connection_id)
    service = MonitoringAutomationService(uow_factory, IMAGE, provider, clock)
    rule = service.create(
        "catalog-team",
        name="daily-quality",
        model_version_id=fields["model_version_id"],
        reference_dataset_id=observed.id,
        observed_dataset_name=observed.name,
        features=["income"],
        minimum_rows=3,
    )
    return service, rule, catalog, connection, provider


def test_publish_dispatch_restart_freezes_snapshot(automation, uow_factory, clock):
    service, rule, catalog, connection, _ = automation
    observed, _ = catalog.publish_dataset(
        "catalog-team", expected_latest_version=1, **spec(connection, checksum_sha256="b" * 64)
    )
    dispatcher = MonitoringDispatcher(uow_factory, clock)
    dispatcher.run_once()
    executions = service.executions("catalog-team", rule.id)
    assert len(executions) == 1
    execution = executions[0]
    assert execution.status == "DISPATCHED" and execution.job_run_id
    assert execution.snapshot["monitoring_spec"]["observed_dataset_id"] == str(observed.id)
    assert execution.snapshot["monitoring_spec"]["reference_dataset_id"] == str(
        rule.reference_dataset_id
    )
    assert "never-return-this" not in json.dumps(execution.snapshot)
    MonitoringDispatcher(uow_factory, clock).run_once()
    assert service.executions("catalog-team", rule.id) == executions
    with uow_factory() as uow:
        assert uow.runs.get(execution.job_run_id).job_definition_id == execution.job_definition_id


def test_pause_only_prevents_future_occurrences_and_revision_is_cas(automation, uow_factory, clock):
    service, rule, catalog, connection, _ = automation
    paused = service.set_enabled("catalog-team", rule.id, enabled=False, revision=1)
    with pytest.raises(Conflict):
        service.set_enabled("catalog-team", rule.id, enabled=True, revision=1)
    catalog.publish_dataset(
        "catalog-team", expected_latest_version=1, **spec(connection, checksum_sha256="b" * 64)
    )
    MonitoringDispatcher(uow_factory, clock).run_once()
    assert service.executions("catalog-team", rule.id) == []
    service.set_enabled("catalog-team", rule.id, enabled=True, revision=paused.revision)
    catalog.publish_dataset(
        "catalog-team", expected_latest_version=2, **spec(connection, checksum_sha256="c" * 64)
    )
    MonitoringDispatcher(uow_factory, clock).run_once()
    assert len(service.executions("catalog-team", rule.id)) == 1


def test_publication_event_rolls_back_with_dataset(automation, uow_factory, clock):
    service, rule, _, connection, _ = automation
    with uow_factory() as uow:
        original = uow.data_catalog.dataset(rule.reference_dataset_id)
        abandoned = replace(
            original, id=uuid4(), version=2, checksum_sha256="b" * 64, created_at=clock()
        )
        uow.data_catalog.add_dataset(abandoned)
        # No commit: neither dataset nor event may survive.
    MonitoringDispatcher(uow_factory, clock).run_once()
    assert service.executions("catalog-team", rule.id) == []
    with uow_factory() as uow:
        assert uow.data_catalog.dataset(abandoned.id) is None


def test_api_hides_internal_metadata_and_supports_pause(automation, uow_factory, clock):
    _, rule, _, _, provider = automation
    with TestClient(create_app(uow_factory, clock, secrets=provider, batch_image=IMAGE)) as client:
        path = "/projects/catalog-team/model-monitoring/rules"
        response = client.get(path)
        assert response.status_code == 200
        assert "secret_metadata" not in response.text
        assert response.json()[0]["name"] == rule.name
        created = client.post(
            path,
            json={
                "name": "api-quality",
                "model_version_id": str(rule.model_version_id),
                "reference_dataset_id": str(rule.reference_dataset_id),
                "observed_dataset_name": rule.observed_dataset_name,
                "features": ["income"],
                "minimum_rows": 3,
            },
        )
        assert created.status_code == 201, created.text
        assert (
            client.patch(
                path + "/" + str(rule.id), json={"enabled": False, "revision": 1}
            ).status_code
            == 200
        )
        assert (
            client.patch(
                path + "/" + str(rule.id), json={"enabled": True, "revision": 1}
            ).status_code
            == 409
        )


def test_waiting_feedback_has_no_run_and_freezes_matching_date(request, uow_factory, clock):
    from datetime import date, timedelta

    from controlplane.domain.data import DatasetColumn

    _, fields, catalog, provider = request.getfixturevalue("batch")
    with uow_factory() as uow:
        original = uow.data_catalog.dataset(fields["input_dataset_id"])
        connection = uow.data_catalog.connection(original.connection_id)
    columns = (
        DatasetColumn("income", "number"),
        DatasetColumn("entity_id", "integer"),
        DatasetColumn("prediction", "number"),
        DatasetColumn("actual", "number"),
    )
    observed, _ = catalog.publish_dataset(
        "catalog-team",
        expected_latest_version=1,
        **spec(
            connection, columns=columns, checksum_sha256="b" * 64, processing_date=date(2025, 1, 1)
        ),
    )
    catalog.publish_dataset(
        "catalog-team",
        **spec(connection, name="ground-truth", columns=columns, processing_date=date(2025, 1, 1)),
    )
    service = MonitoringAutomationService(uow_factory, IMAGE, provider, clock)
    rule = service.create(
        "catalog-team",
        name="feedback-quality",
        model_version_id=fields["model_version_id"],
        reference_dataset_id=observed.id,
        observed_dataset_name=observed.name,
        feedback_dataset_name="ground-truth",
        features=["income"],
        minimum_rows=3,
        entity_key="entity_id",
    )
    new, _ = catalog.publish_dataset(
        "catalog-team",
        expected_latest_version=2,
        **spec(
            connection, columns=columns, checksum_sha256="c" * 64, processing_date=date(2025, 2, 1)
        ),
    )
    MonitoringDispatcher(uow_factory, clock).run_once()
    waiting = service.executions("catalog-team", rule.id)[0]
    assert waiting.status == "WAITING_FEEDBACK" and waiting.job_run_id is None
    # Pausing stops future triggers, not the already durable execution.
    service.set_enabled("catalog-team", rule.id, enabled=False, revision=1)
    truth, _ = catalog.publish_dataset(
        "catalog-team",
        expected_latest_version=1,
        **spec(
            connection,
            name="ground-truth",
            columns=columns,
            checksum_sha256="d" * 64,
            processing_date=date(2025, 2, 1),
        ),
    )
    clock.advance(timedelta(seconds=20))
    MonitoringDispatcher(uow_factory, clock).run_once()
    dispatched = service.executions("catalog-team", rule.id)[0]
    assert dispatched.id == waiting.id and dispatched.status == "DISPATCHED"
    assert dispatched.snapshot["monitoring_spec"]["feedback_dataset_id"] == str(truth.id)
    assert dispatched.snapshot["monitoring_spec"]["observed_dataset_id"] == str(new.id)


def test_dispatch_validation_failure_preserves_published_dataset(automation, uow_factory, clock):
    from controlplane.domain.data import DatasetColumn

    service, rule, catalog, connection, _ = automation
    dataset, _ = catalog.publish_dataset(
        "catalog-team",
        expected_latest_version=1,
        **spec(connection, columns=(DatasetColumn("income", "string"),), checksum_sha256="b" * 64),
    )
    MonitoringDispatcher(uow_factory, clock).run_once()
    execution = service.executions("catalog-team", rule.id)[0]
    assert execution.status == "FAILED" and execution.job_run_id is None
    with uow_factory() as uow:
        assert uow.data_catalog.dataset(dataset.id) == dataset
        assert uow.jobs.get_by_name(dataset.project_id, "mon-" + execution.id.hex) is None


def test_failed_commit_rolls_back_run_and_intent_before_retry(automation, uow_factory, clock):
    service, rule, catalog, connection, _ = automation
    catalog.publish_dataset(
        "catalog-team", expected_latest_version=1, **spec(connection, checksum_sha256="b" * 64)
    )

    class CrashCommit:
        def __enter__(self):
            self.uow = uow_factory()
            self.uow.__enter__()
            self.uow.commit = lambda: (_ for _ in ()).throw(RuntimeError("crash before commit"))
            return self.uow

        def __exit__(self, *args):
            return self.uow.__exit__(*args)

    with pytest.raises(RuntimeError, match="crash before commit"):
        MonitoringDispatcher(CrashCommit, clock).run_once()
    assert service.executions("catalog-team", rule.id) == []
    MonitoringDispatcher(uow_factory, clock).run_once()
    assert len(service.executions("catalog-team", rule.id)) == 1


def test_two_postgres_dispatchers_create_single_run(automation, uow_factory, clock):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    service, rule, catalog, connection, _ = automation
    with uow_factory() as uow:
        if not hasattr(uow, "_session"):
            pytest.skip("row locking requires PostgreSQL")
    catalog.publish_dataset(
        "catalog-team", expected_latest_version=1, **spec(connection, checksum_sha256="b" * 64)
    )
    now = clock()
    barrier = Barrier(2)

    def dispatch():
        barrier.wait(timeout=10)
        return MonitoringDispatcher(uow_factory, lambda: now).run_once()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(dispatch) for _ in range(2)]
        for future in futures:
            future.result(timeout=20)
    MonitoringDispatcher(uow_factory, clock).run_once()
    assert len(service.executions("catalog-team", rule.id)) == 1
