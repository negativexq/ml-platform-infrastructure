from types import SimpleNamespace as NS
from unittest.mock import MagicMock

import pytest
from kubernetes.client.exceptions import ApiException

from controlplane.adapters.workflow.argo import ArgoWorkflowProvider, build_workflow
from controlplane.application.providers import StepSpec, WorkflowSpec
from controlplane.application.worker_resources import (
    disk_requirement,
    storage_bytes,
    worker_resources,
)
from controlplane.domain.errors import InvalidArgument

GIB = 1024**3


def test_disk_budget_includes_retained_data_scratch_and_feedback_journal():
    assert disk_requirement(batch={"max_bytes": 2 * GIB, "max_model_bytes": GIB}) == 5632 * 1024**2
    assert (
        disk_requirement(
            monitoring={
                "max_bytes": 2 * GIB,
                "max_join_bytes": 2 * GIB,
                "feedback_dataset_id": "id",
            }
        )
        == 11 * GIB
    )
    assert disk_requirement(monitoring={"max_bytes": GIB, "max_join_bytes": GIB}) == 2560 * 1024**2


@pytest.mark.parametrize(
    "quantity,expected",
    [("1.5Gi", 1610612736), ("1536Mi", 1610612736), ("1G", 1000000000), ("500m", 1)],
)
def test_storage_quantities(quantity, expected):
    assert storage_bytes(quantity) == expected


@pytest.mark.parametrize("disk", ["1Gi", "16Gi", "32Gi", "invalid", "0"])
def test_undersized_or_impossible_worker_is_rejected(disk):
    with pytest.raises(InvalidArgument):
        worker_resources(
            {"cpu": "500m", "ephemeral-storage": disk},
            batch={"max_bytes": GIB, "max_model_bytes": GIB // 2},
        )


def provider(node="10Gi", quota="16Gi", existing=False):
    value = ArgoWorkflowProvider(None)
    value._custom = MagicMock()
    if not existing:
        value._custom.get_namespaced_custom_object.side_effect = ApiException(status=404)
    value._core = MagicMock()
    value._core.list_namespaced_limit_range.return_value = NS(
        items=[
            NS(
                spec=NS(
                    limits=[
                        NS(
                            type="Container",
                            default_request={"ephemeral-storage": "256Mi"},
                            default={"ephemeral-storage": "2Gi"},
                        )
                    ]
                )
            )
        ]
    )
    value._core.list_namespaced_resource_quota.return_value = NS(
        items=[
            NS(
                spec=NS(
                    hard={"requests.ephemeral-storage": quota, "limits.ephemeral-storage": "32Gi"}
                )
            )
        ]
    )
    value._core.list_node.return_value = NS(
        items=[NS(status=NS(allocatable={"ephemeral-storage": node}))]
    )
    return value


def workflow():
    return WorkflowSpec(
        "run",
        "mlp-team",
        (StepSpec("main", "train@sha256:" + "a" * 64, resources={"ephemeral-storage": "3Gi"}),),
        {},
    )


@pytest.mark.parametrize("node,quota", [("3Gi", "16Gi"), ("10Gi", "3Gi")])
def test_preflight_rejects_impossible_pod_before_workflow_creation(node, quota):
    value = provider(node, quota)
    with pytest.raises(InvalidArgument):
        value.submit(workflow(), "run")
    value._custom.create_namespaced_custom_object.assert_not_called()


def test_preflight_includes_executor_and_does_not_reserve_current_free_capacity():
    value = provider()
    assert value.submit(workflow(), "run") == "mlp-team/run"
    container = build_workflow(workflow())["spec"]["templates"][0]["container"]
    assert container["resources"] == {
        "requests": {"ephemeral-storage": "3Gi"},
        "limits": {"ephemeral-storage": "3Gi"},
    }
    value._custom.create_namespaced_custom_object.assert_called_once()


def test_committed_submit_is_adopted_even_when_capacity_changes():
    value = provider(node="1Gi", existing=True)
    assert value.submit(workflow(), "run") == "mlp-team/run"
    value._core.list_node.assert_not_called()
    value._custom.create_namespaced_custom_object.assert_not_called()
