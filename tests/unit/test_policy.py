import json

import responses
from fastapi.testclient import TestClient

from webhook.handlers import policy
from webhook.main import app
from webhook.netbird_client import NetBirdClient, Resolver


SPEC = {
    "name": "location-access",
    "description": "location resources",
    "enabled": True,
    "sourceGroupRefs": ["jwt-vpn-users"],
    "destinationGroupRefs": ["network-resources"],
    "sourcePostureCheckRefs": ["minimum-version"],
    "rules": [
        {
            "name": "web",
            "enabled": True,
            "action": "accept",
            "bidirectional": False,
            "protocol": "tcp",
            "ports": [80, 443],
        },
        {
            "name": "range",
            "enabled": True,
            "action": "accept",
            "bidirectional": False,
            "protocol": "udp",
            "portRanges": [{"start": 9100, "end": 9200}],
        },
        {
            "name": "ping",
            "enabled": True,
            "action": "accept",
            "bidirectional": False,
            "protocol": "icmp",
        },
    ],
}


def _resolver():
    return Resolver(NetBirdClient())


def _stub_ref_data():
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-src", "name": "jwt-vpn-users", "issued": "jwt"}],
        match=[responses.matchers.query_param_matcher({"name": "jwt-vpn-users"})],
    )
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "g-dst", "name": "network-resources", "issued": "api"}],
        match=[responses.matchers.query_param_matcher({"name": "network-resources"})],
    )
    responses.get(
        "https://nb.test/api/posture-checks",
        json=[{"id": "pc1", "name": "minimum-version"}],
    )


@responses.activate
def test_create_resolves_jwt_groups_posture_checks_and_full_rules():
    responses.get("https://nb.test/api/policies", json=[])
    _stub_ref_data()
    responses.post("https://nb.test/api/policies", json={"id": "p1"})

    assert policy.sync({"spec": SPEC, "status": {}}, _resolver()) == {"id": "p1"}
    body = json.loads(next(call for call in responses.calls if call.request.method == "POST").request.body)
    assert body["source_posture_checks"] == ["pc1"]
    assert all(rule["sources"] == ["g-src"] for rule in body["rules"])
    assert all(rule["destinations"] == ["g-dst"] for rule in body["rules"])
    assert body["rules"][0]["ports"] == ["80", "443"]
    assert body["rules"][1]["port_ranges"] == [{"start": 9100, "end": 9200}]
    assert body["rules"][2]["protocol"] == "icmp"


@responses.activate
def test_adopts_default_by_name_and_disables_without_replacing_rule_id():
    current = {
        "id": "default-id",
        "name": "Default",
        "description": "default",
        "enabled": True,
        "source_posture_checks": [],
        "rules": [
            {
                "id": "rule-id",
                "name": "Default",
                "enabled": True,
                "action": "accept",
                "bidirectional": True,
                "protocol": "all",
                "sources": [{"id": "all", "name": "All"}],
                "destinations": [{"id": "all", "name": "All"}],
            }
        ],
    }
    responses.get("https://nb.test/api/policies", json=[current])
    responses.get(
        "https://nb.test/api/groups",
        json=[{"id": "all", "name": "All"}],
        match=[responses.matchers.query_param_matcher({"name": "All"})],
    )
    responses.put("https://nb.test/api/policies/default-id", json=current)
    spec = {
        "name": "Default",
        "description": "default",
        "enabled": False,
        "sourceGroupRefs": ["All"],
        "destinationGroupRefs": ["All"],
        "rules": [{"name": "Default", "protocol": "all"}],
    }

    assert policy.sync({"spec": spec, "status": {}}, _resolver()) == {"id": "default-id"}
    body = json.loads(next(call for call in responses.calls if call.request.method == "PUT").request.body)
    assert body["enabled"] is False
    assert body["rules"][0]["id"] == "rule-id"


@responses.activate
def test_state_absent_adopts_then_deletes_only_persisted_id():
    spec = {"name": "Default", "state": "absent"}
    responses.get("https://nb.test/api/policies", json=[{"id": "default-id", "name": "Default"}])
    first = policy.sync({"spec": spec, "status": {}}, _resolver())
    assert first["id"] == "default-id"
    assert first["pendingDeletion"] is True
    assert not any(call.request.method == "DELETE" for call in responses.calls)

    responses.reset()
    responses.delete("https://nb.test/api/policies/default-id", status=204)
    second = policy.sync({"spec": spec, "status": {"id": "default-id"}}, _resolver())
    assert second == {"state": "Absent"}


@responses.activate
def test_finalize_without_owned_id_does_not_search_or_delete():
    assert policy.finalize({"spec": {"name": "Default"}, "status": {}}, _resolver()) == {}
    assert not responses.calls


@responses.activate
def test_webhook_adds_observed_generation_and_conditions():
    responses.get("https://nb.test/api/policies", json=[])
    result = TestClient(app).post(
        "/sync/policy",
        json={"parent": {"metadata": {"generation": 7}, "spec": {"name": "gone", "state": "absent"}}},
    ).json()
    assert result["status"]["observedGeneration"] == 7
    assert result["status"]["conditions"][0]["type"] == "Synced"


def test_rejects_ports_for_icmp():
    spec = dict(SPEC)
    spec["rules"] = [{"name": "bad", "protocol": "icmp", "ports": [8]}]
    try:
        policy._desired(spec, _resolver())
    except ValueError as exc:
        assert "cannot declare ports" in str(exc)
    else:
        raise AssertionError("expected ValueError")
