import json

import responses
from fastapi.testclient import TestClient

from webhook.handlers import network
from webhook.main import app
from webhook.netbird_client import NetBirdClient, Resolver


def _resolver():
    return Resolver(NetBirdClient())


@responses.activate
def test_create_and_adopt_by_name():
    responses.get("https://nb.test/api/networks", json=[])
    responses.post("https://nb.test/api/networks", json={"id": "n1"})
    parent = {"spec": {"name": "location-secondary-site", "description": "HQ", "enabled": True}, "status": {}}
    assert network.sync(parent, _resolver()) == {"id": "n1"}
    body = json.loads(next(call for call in responses.calls if call.request.method == "POST").request.body)
    assert body == {"name": "location-secondary-site", "description": "HQ"}

    responses.reset()
    responses.get("https://nb.test/api/networks", json=[{"id": "n1", "name": "location-secondary-site", "description": "HQ"}])
    assert network.sync(parent, _resolver()) == {"id": "n1"}
    assert not any(call.request.method in {"POST", "PUT"} for call in responses.calls)


def test_enabled_false_is_rejected_without_destructive_fallback():
    try:
        network.sync({"spec": {"name": "location-secondary-site", "enabled": False}}, _resolver())
    except ValueError as exc:
        assert "no enabled state" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_enabled_false_is_reported_as_invalid_spec_by_webhook():
    result = TestClient(app).post(
        "/sync/network",
        json={"parent": {"metadata": {"generation": 3}, "spec": {"name": "location-secondary-site", "enabled": False}}},
    ).json()
    assert result["status"]["synced"] is False
    assert result["status"]["observedGeneration"] == 3
    assert result["status"]["conditions"][0]["reason"] == "InvalidSpec"


@responses.activate
def test_finalize_uses_only_status_id():
    assert network.finalize({"spec": {"name": "location-secondary-site"}, "status": {}}, _resolver()) == {}
    assert not responses.calls
    responses.delete("https://nb.test/api/networks/n1", status=404)
    assert network.finalize({"status": {"id": "n1"}}, _resolver()) == {}
