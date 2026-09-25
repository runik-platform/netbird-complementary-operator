import json

import responses

from webhook.handlers import dns_settings
from webhook.netbird_client import NetBirdClient, Resolver


@responses.activate
def test_sync_updates_when_changed():
    responses.get(
        "https://nb.test/api/dns/settings",
        json={"disabled_management_groups": ["old"]},
        status=200,
    )
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g1", "name": "admins"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "admins"})],
    )
    responses.put("https://nb.test/api/dns/settings", json={}, status=200)

    parent = {"spec": {"disabledManagementGroupRefs": ["admins"]}}
    dns_settings.sync(parent, Resolver(NetBirdClient()))

    put_body = json.loads(responses.calls[-1].request.body)
    assert put_body == {"disabled_management_groups": ["g1"]}


@responses.activate
def test_sync_noop_when_unchanged():
    responses.get(
        "https://nb.test/api/dns/settings",
        json={"disabled_management_groups": ["g1"]},
        status=200,
    )
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g1", "name": "admins"}],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "admins"})],
    )

    parent = {"spec": {"disabledManagementGroupRefs": ["admins"]}}
    dns_settings.sync(parent, Resolver(NetBirdClient()))
    # No PUT call recorded
    methods = [c.request.method for c in responses.calls]
    assert "PUT" not in methods
