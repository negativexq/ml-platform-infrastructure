import pytest

from controlplane.adapters.kubernetes.networking import NetworkTopology, project_policies


def test_serving_ingress_does_not_open_training_pods() -> None:
    policies = project_policies("mlp-a", {}, NetworkTopology(system_namespace="custom-system"))
    baseline = policies["networkpolicy"]["spec"]
    serving = policies["servingnetworkpolicy"]["spec"]
    assert baseline["ingress"] == [{"from": [{"podSelector": {}}]}]
    assert (
        serving["podSelector"]["matchExpressions"][0]["key"] == "serving.kserve.io/inferenceservice"
    )
    callers = serving["ingress"][0]["from"]
    assert (
        callers[0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
        == "custom-system"
    )
    assert not any(
        p.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name")
        == "mlp-b"
        for p in callers
    )


def test_egress_requires_site_api_endpoints_and_no_cross_project_peer() -> None:
    with pytest.raises(ValueError, match="API endpoint"):
        NetworkTopology(isolate_egress=True)
    topology = NetworkTopology(isolate_egress=True, api_cidrs=("10.96.0.1/32",))
    egress = project_policies("mlp-a", {}, topology)["egressnetworkpolicy"]["spec"]["egress"]
    assert any(rule["to"] == [{"ipBlock": {"cidr": "10.96.0.1/32"}}] for rule in egress)
    assert all(peer.get("namespaceSelector") != {} for rule in egress for peer in rule["to"])
    assert not any("0.0.0.0/0" in str(rule) for rule in egress)


def test_empty_serving_namespaces_does_not_allow_all_egress() -> None:
    topology = NetworkTopology(
        isolate_egress=True, api_cidrs=("10.96.0.1/32",), serving_namespaces=()
    )
    egress = project_policies("mlp-a", {}, topology)["egressnetworkpolicy"]["spec"]["egress"]
    assert all(rule["to"] for rule in egress)
