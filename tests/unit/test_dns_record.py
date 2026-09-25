import json

import responses

from webhook.handlers import dns_record
from webhook.netbird_client import NetBirdClient, Resolver


SPEC = {
    "zoneRef": "internal",
    "name": "web.internal.example",
    "type": "A",
    "content": "10.0.0.5",
    "ttl": 300,
}


@responses.activate
def test_creates_in_zone():
    responses.get("https://nb.test/api/dns/zones", json=[{"id": "z1", "name": "internal"}], status=200)
    responses.get("https://nb.test/api/dns/zones/z1/records", json=[], status=200)
    responses.post("https://nb.test/api/dns/zones/z1/records", json={"id": "r1"}, status=200)
    parent = {"spec": SPEC, "status": {}}
    out = dns_record.sync(parent, Resolver(NetBirdClient()))
    assert out == {"id": "r1", "zoneId": "z1"}
    body = json.loads(next(c for c in responses.calls if c.request.method == "POST").request.body)
    assert body == {"name": SPEC["name"], "type": "A", "content": "10.0.0.5", "ttl": 300}


@responses.activate
def test_updates_when_content_changes():
    responses.get("https://nb.test/api/dns/zones", json=[{"id": "z1", "name": "internal"}], status=200)
    responses.get(
        "https://nb.test/api/dns/zones/z1/records/r1",
        json={"id": "r1", "name": SPEC["name"], "type": "A", "content": "10.0.0.99", "ttl": 300},
        status=200,
    )
    responses.put("https://nb.test/api/dns/zones/z1/records/r1", json={"id": "r1"}, status=200)
    parent = {"spec": SPEC, "status": {"id": "r1", "zoneId": "z1"}}
    dns_record.sync(parent, Resolver(NetBirdClient()))
    put = next(c for c in responses.calls if c.request.method == "PUT")
    assert json.loads(put.request.body)["content"] == "10.0.0.5"


@responses.activate
def test_finalize_tolerates_missing_zone():
    responses.get("https://nb.test/api/dns/zones", status=401, body='{"message":"token invalid"}')
    parent = {"spec": SPEC, "status": {}}
    assert dns_record.finalize(parent, Resolver(NetBirdClient())) == {}
