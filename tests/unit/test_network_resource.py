import json

import responses

from webhook.handlers import network_resource
from webhook.netbird_client import NetBirdClient, Resolver


def _resolver():
    return Resolver(NetBirdClient())


def _stub_refs():
    responses.get("https://nb.test/api/networks", json=[{"id": "n1", "name": "location"}])
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g1", "name": "resources"}],
        match=[responses.matchers.query_param_matcher({"name": "resources"})],
    )


@responses.activate
def test_create_arbitrary_resource_and_resolve_network_and_group():
    _stub_refs()
    responses.get("https://nb.test/api/networks/n1/resources", json=[])
    responses.post("https://nb.test/api/networks/n1/resources", json={"id": "res1"})
    spec = {
        "networkRef": {"name": "location"},
        "name": "grafana",
        "description": "dashboard",
        "address": "*.internal.example",
        "enabled": True,
        "groupRefs": ["resources"],
    }
    assert network_resource.sync({"spec": spec, "status": {}}, _resolver()) == {"id": "res1", "networkId": "n1"}
    body = json.loads(next(call for call in responses.calls if call.request.method == "POST").request.body)
    assert body["address"] == "*.internal.example"
    assert body["groups"] == ["g1"]


@responses.activate
def test_noop_normalizes_group_objects_and_finalize_uses_owned_location():
    _stub_refs()
    responses.get(
        "https://nb.test/api/networks/n1/resources/res1",
        json={"id": "res1", "name": "host", "description": "", "address": "10.0.0.1/32", "enabled": True, "groups": [{"id": "g1", "name": "resources"}]},
    )
    spec = {"networkRef": {"name": "location"}, "name": "host", "address": "10.0.0.1/32", "groupRefs": ["resources"]}
    out = network_resource.sync({"spec": spec, "status": {"id": "res1", "networkId": "n1"}}, _resolver())
    assert out == {"id": "res1", "networkId": "n1"}
    assert not any(call.request.method == "PUT" for call in responses.calls)

    responses.reset()
    responses.delete("https://nb.test/api/networks/n1/resources/res1", status=204)
    assert network_resource.finalize({"status": out}, _resolver()) == {}
