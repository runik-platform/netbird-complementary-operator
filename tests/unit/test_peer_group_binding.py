import json

import responses

from webhook.handlers import peer_group_binding
from webhook.netbird_client import NetBirdClient, Resolver


def _resolver():
    return Resolver(NetBirdClient())


def _stub_refs(peers=None):
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g1", "name": "routers"}],
        match=[responses.matchers.query_param_matcher({"name": "routers"})],
    )
    responses.get("https://nb.test/api/peers", json=peers if peers is not None else [{"id": "p1", "name": "router-01"}])


@responses.activate
def test_adds_only_target_membership_and_preserves_everything_else():
    _stub_refs()
    responses.get(
        "https://nb.test/api/groups/g1",
        json={
            "id": "g1",
            "name": "routers",
            "peers": [{"id": "other", "name": "other-router"}],
            "resources": [{"id": "res1", "type": "subnet"}],
        },
    )
    responses.put("https://nb.test/api/groups/g1", json={"id": "g1"})
    parent = {"spec": {"peerRef": "router-01", "groupRef": "routers"}, "status": {}}
    assert peer_group_binding.sync(parent, _resolver()) == {"groupId": "g1", "peerId": "p1", "managed": True}
    body = json.loads(next(call for call in responses.calls if call.request.method == "PUT").request.body)
    assert body["peers"] == ["other", "p1"]
    assert body["resources"] == [{"id": "res1", "type": "subnet"}]


@responses.activate
def test_missing_peer_waits_without_mutating_group():
    _stub_refs(peers=[])
    try:
        peer_group_binding.sync(
            {"spec": {"peerRef": "router-01", "groupRef": "routers"}, "status": {}},
            _resolver(),
        )
    except KeyError as exc:
        assert "peer not found" in str(exc)
    else:
        raise AssertionError("expected KeyError")
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_finalize_removes_only_owned_peer_membership():
    responses.get(
        "https://nb.test/api/groups/g1",
        json={
            "id": "g1",
            "name": "routers",
            "peers": [{"id": "p1"}, {"id": "other"}],
            "resources": [{"id": "res1", "type": "host"}],
        },
    )
    responses.put("https://nb.test/api/groups/g1", json={"id": "g1"})
    assert peer_group_binding.finalize(
        {"status": {"groupId": "g1", "peerId": "p1", "managed": True}}, _resolver()
    ) == {}
    body = json.loads(next(call for call in responses.calls if call.request.method == "PUT").request.body)
    assert body["peers"] == ["other"]
    assert body["resources"] == [{"id": "res1", "type": "host"}]
