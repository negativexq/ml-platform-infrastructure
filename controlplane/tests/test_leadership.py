from copy import deepcopy
from unittest.mock import MagicMock

from kubernetes import client
from kubernetes.client.exceptions import ApiException

from controlplane.adapters.kubernetes.leadership import LeaseLeadership


def test_standby_waits_for_expiry_and_conflicts_do_not_grant_ownership() -> None:
    current = client.V1Lease(
        metadata=client.V1ObjectMeta(name="leader", resource_version="1"),
        spec=client.V1LeaseSpec(holder_identity="first", lease_duration_seconds=30),
    )
    api, fatal = MagicMock(), MagicMock()
    api.read_namespaced_lease.side_effect = lambda *a, **k: deepcopy(current)
    now = [0.0]
    standby = LeaseLeadership(
        api, "system", "leader", identity="second", clock=lambda: now[0], fatal=fatal
    )
    assert not standby.attempt()
    now[0] = 29
    assert not standby.attempt()
    now[0] = 31
    api.replace_namespaced_lease.side_effect = ApiException(status=409)
    assert not standby.attempt() and not standby.active
    api.replace_namespaced_lease.side_effect = None
    assert standby.attempt() and standby.active
    assert api.replace_namespaced_lease.call_args.args[2].metadata.resource_version == "1"
    current.spec.holder_identity = "third"
    assert not standby.attempt()
    fatal.assert_called_once()


def test_renewal_outage_stops_leader_at_deadline() -> None:
    api, fatal = MagicMock(), MagicMock()
    lease = client.V1Lease(
        metadata=client.V1ObjectMeta(resource_version="1"),
        spec=client.V1LeaseSpec(holder_identity="self"),
    )
    api.read_namespaced_lease.return_value = lease
    now = [0.0]
    leader = LeaseLeadership(
        api, "system", "leader", identity="self", clock=lambda: now[0], fatal=fatal
    )
    assert leader.attempt()
    api.read_namespaced_lease.side_effect = TimeoutError()

    def wait(seconds: float) -> bool:
        now[0] += seconds
        return now[0] > 20

    leader.stop = MagicMock(wait=MagicMock(side_effect=wait))
    leader._renew()
    assert not leader.active
    fatal.assert_called_once()
