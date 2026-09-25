import json

import responses

from webhook.handlers import dns_zone
from webhook.netbird_client import NetBirdClient, Resolver


@responses.activate
def test_creates_resolves_distribution_groups():
    responses.get("https://nb.test/api/dns/zones", json=[], status=200)
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-all", "name": "All"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "All"})],
    )
    responses.post("https://nb.test/api/dns/zones", json={"id": "z1"}, status=200)
    parent = {
        "spec": {"name": "internal", "domain": "internal.example.com",
                 "distributionGroupRefs": ["All"], "enableSearchDomain": True},
        "status": {},
    }
    out = dns_zone.sync(parent, Resolver(NetBirdClient()))
    assert out == {"id": "z1"}
    body = json.loads(next(c for c in responses.calls if c.request.method == "POST").request.body)
    assert body == {
        "name": "internal",
        "domain": "internal.example.com",
        "enabled": True,
        "enable_search_domain": True,
        "distribution_groups": ["g-all"],
    }


@responses.activate
def test_noop_when_equal():
    current = {
        "id": "z1",
        "name": "internal",
        "domain": "internal.example.com",
        "enabled": True,
        "enable_search_domain": False,
        "distribution_groups": ["g-all"],
    }
    responses.get("https://nb.test/api/dns/zones/z1", json=current, status=200)
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-all", "name": "All"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "All"})],
    )
    parent = {
        "spec": {"name": "internal", "domain": "internal.example.com",
                 "distributionGroupRefs": ["All"]},
        "status": {"id": "z1"},
    }
    dns_zone.sync(parent, Resolver(NetBirdClient()))
    assert not any(c.request.method == "PUT" for c in responses.calls)


@responses.activate
def test_finalize_tolerates_404():
    responses.delete("https://nb.test/api/dns/zones/z1", status=404)
    parent = {"spec": {"name": "internal", "domain": "x"}, "status": {"id": "z1"}}
    assert dns_zone.finalize(parent, Resolver(NetBirdClient())) == {}
