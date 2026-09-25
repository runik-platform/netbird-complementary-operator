import json

import responses

from webhook.handlers import peer
from webhook.netbird_client import NetBirdClient, Resolver


@responses.activate
def test_sync_updates_existing_peer_settings():
    responses.get(
        "https://nb.test/api/peers",
        json=[{"id": "p1", "name": "server-01"}],
        status=200,
    )
    responses.get(
        "https://nb.test/api/peers/p1",
        json={"id": "p1", "name": "server-01", "ssh_enabled": True,
              "login_expiration_enabled": True, "inactivity_expiration_enabled": True},
        status=200,
    )
    responses.put("https://nb.test/api/peers/p1", json={"id": "p1"}, status=200)
    parent = {
        "spec": {
            "peerRef": "server-01",
            "sshEnabled": False,
            "loginExpirationEnabled": False,
            "inactivityExpirationEnabled": False,
        },
        "status": {},
    }
    out = peer.sync(parent, Resolver(NetBirdClient()))
    assert out == {"id": "p1"}
    put = next(c for c in responses.calls if c.request.method == "PUT")
    body = json.loads(put.request.body)
    assert body["ssh_enabled"] is False
    assert body["login_expiration_enabled"] is False
    assert body["inactivity_expiration_enabled"] is False


@responses.activate
def test_sync_raises_when_peer_not_registered():
    responses.get("https://nb.test/api/peers", json=[], status=200)
    parent = {"spec": {"peerRef": "ghost"}, "status": {}}
    try:
        peer.sync(parent, Resolver(NetBirdClient()))
    except KeyError as e:
        assert "ghost" in str(e)
    else:
        raise AssertionError
