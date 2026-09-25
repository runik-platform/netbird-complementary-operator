import json

import responses

from webhook.handlers import nameserver_group
from webhook.netbird_client import NetBirdClient, Resolver


SPEC = {
    "name": "cloudflare",
    "description": "cf",
    "nameservers": [
        {"ip": "1.1.1.1", "nsType": "udp", "port": 53},
        {"ip": "1.0.0.1", "nsType": "udp", "port": 53},
    ],
    "groupRefs": ["All"],
    "primary": True,
    "domains": [],
    "enabled": True,
    "searchDomainsEnabled": False,
}


@responses.activate
def test_sync_creates_when_missing():
    responses.get("https://nb.test/api/dns/nameservers", json=[], status=200)
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-all", "name": "All"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "All"})],
    )
    responses.post(
        "https://nb.test/api/dns/nameservers",
        json={"id": "ns-1", **_expected_body()},
        status=200,
    )

    parent = {"spec": SPEC, "status": {}}
    out = nameserver_group.sync(parent, Resolver(NetBirdClient()))
    assert out == {"id": "ns-1"}

    post_body = json.loads(next(c for c in responses.calls if c.request.method == "POST").request.body)
    assert post_body == _expected_body()


@responses.activate
def test_sync_updates_when_diff():
    current = {"id": "ns-1", **_expected_body()}
    current["enabled"] = False
    responses.get("https://nb.test/api/dns/nameservers/ns-1", json=current, status=200)
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-all", "name": "All"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "All"})],
    )
    responses.put("https://nb.test/api/dns/nameservers/ns-1", json={"id": "ns-1"}, status=200)

    parent = {"spec": SPEC, "status": {"id": "ns-1"}}
    nameserver_group.sync(parent, Resolver(NetBirdClient()))
    put_calls = [c for c in responses.calls if c.request.method == "PUT"]
    assert len(put_calls) == 1


@responses.activate
def test_sync_noop_when_equal():
    current = {"id": "ns-1", **_expected_body()}
    responses.get("https://nb.test/api/dns/nameservers/ns-1", json=current, status=200)
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-all", "name": "All"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "All"})],
    )

    parent = {"spec": SPEC, "status": {"id": "ns-1"}}
    nameserver_group.sync(parent, Resolver(NetBirdClient()))
    assert not any(c.request.method == "PUT" for c in responses.calls)


@responses.activate
def test_finalize_deletes_by_id_and_tolerates_404():
    responses.delete("https://nb.test/api/dns/nameservers/ns-1", status=404)
    parent = {"spec": SPEC, "status": {"id": "ns-1"}}
    out = nameserver_group.finalize(parent, Resolver(NetBirdClient()))
    assert out == {}


def _expected_body() -> dict:
    return {
        "name": "cloudflare",
        "description": "cf",
        "nameservers": [
            {"ip": "1.1.1.1", "ns_type": "udp", "port": 53},
            {"ip": "1.0.0.1", "ns_type": "udp", "port": 53},
        ],
        "enabled": True,
        "groups": ["g-all"],
        "primary": True,
        "domains": [],
        "search_domains_enabled": False,
    }
