import json

import responses

from webhook.handlers import route
from webhook.netbird_client import NetBirdClient, Resolver


SPEC = {
    "description": "Home LAN",
    "networkId": "home",
    "enabled": True,
    "peerGroupRefs": ["vpn-servers"],
    "network": "10.10.0.0/24",
    "metric": 100,
    "masquerade": True,
    "groupRefs": ["vpn-users"],
    "keepRoute": True,
}


def _stub_groups():
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-srv", "name": "vpn-servers"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "vpn-servers"})],
    )
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-usr", "name": "vpn-users"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "vpn-users"})],
    )


@responses.activate
def test_sync_creates_when_missing():
    _stub_groups()
    responses.get("https://nb.test/api/routes", json=[], status=200)
    responses.post(
        "https://nb.test/api/routes",
        json={"id": "r1"},
        status=200,
    )

    parent = {"spec": SPEC, "status": {}}
    out = route.sync(parent, Resolver(NetBirdClient()))
    assert out == {"id": "r1"}
    post = next(c for c in responses.calls if c.request.method == "POST")
    body = json.loads(post.request.body)
    assert body["network"] == "10.10.0.0/24"
    assert body["peer_groups"] == ["g-srv"]
    assert body["groups"] == ["g-usr"]
    assert body["description"] == "Home LAN"
    assert body["network_id"] == "home"


@responses.activate
def test_finalize_deletes_and_tolerates_404():
    responses.delete("https://nb.test/api/routes/r1", status=404)
    parent = {"spec": SPEC, "status": {"id": "r1"}}
    assert route.finalize(parent, Resolver(NetBirdClient())) == {}


@responses.activate
def test_mutex_peer_and_group_refs():
    bad = dict(SPEC)
    bad["peerRef"] = "host1"
    try:
        route._desired(bad, Resolver(NetBirdClient()))
    except ValueError as e:
        assert "mutually exclusive" in str(e)
    else:
        raise AssertionError("expected ValueError")
