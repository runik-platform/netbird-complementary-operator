import json

import responses

from webhook.handlers import network_router
from webhook.netbird_client import NetBirdClient, Resolver


def _resolver():
    return Resolver(NetBirdClient())


@responses.activate
def test_create_router_for_existing_peer_group():
    responses.get("https://nb.test/api/networks", json=[{"id": "n1", "name": "location"}])
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-router", "name": "routers"}],
        match=[responses.matchers.query_param_matcher({"name": "routers"})],
    )
    responses.get("https://nb.test/api/networks/n1/routers", json=[])
    responses.post("https://nb.test/api/networks/n1/routers", json={"id": "r1"})
    spec = {
        "networkRef": {"name": "location"},
        "peerGroupRefs": ["routers"],
        "enabled": True,
        "masquerade": True,
        "metric": 100,
    }
    assert network_router.sync({"spec": spec, "status": {}}, _resolver()) == {"id": "r1", "networkId": "n1"}
    body = json.loads(next(call for call in responses.calls if call.request.method == "POST").request.body)
    assert body == {"peer_groups": ["g-router"], "enabled": True, "masquerade": True, "metric": 100}


def test_router_requires_exactly_one_peer_selector():
    for spec in (
        {"networkRef": {"name": "n"}},
        {"networkRef": {"name": "n"}, "peerRef": "p", "peerGroupRefs": ["g"]},
    ):
        try:
            network_router._desired(spec, _resolver())
        except ValueError:
            pass
        else:
            raise AssertionError("expected ValueError")


@responses.activate
def test_finalize_never_rediscovers_router():
    assert network_router.finalize({"status": {}}, _resolver()) == {}
    assert not responses.calls
    responses.delete("https://nb.test/api/networks/n1/routers/r1", status=404)
    assert network_router.finalize({"status": {"id": "r1", "networkId": "n1"}}, _resolver()) == {}
