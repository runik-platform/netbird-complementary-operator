import json

import responses
from fastapi.testclient import TestClient

from webhook.handlers import role_binding
from webhook.main import app
from webhook.netbird_client import NetBirdClient, Resolver

GROUP = {"id": "g-admin", "name": "vpn-admins"}


def parent(*, status=None, name="vpn-admins", **spec_overrides):
    spec = {
        "groupRef": {"name": "vpn-admins"},
        "role": "admin",
        "revokeOnExit": True,
        "restoreOnDelete": True,
        "includeServiceUsers": False,
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "netbird.io/v1alpha1",
        "kind": "NetBirdRoleBinding",
        "metadata": {"name": name, "namespace": "netbird", "generation": 2},
        "spec": spec,
        "status": status or {},
    }


def user(user_id="u1", role="user", groups=None, service=False, name=None):
    return {
        "id": user_id,
        "name": name or user_id,
        "role": role,
        "auto_groups": ["g-admin"] if groups is None else groups,
        "is_service_user": service,
        "is_blocked": False,
    }


def stub_group(group=GROUP):
    responses.get(
        "https://nb.test/api/groups",
        json=[] if group is None else [group],
        status=200,
        match=[responses.matchers.query_param_matcher({"name": "vpn-admins"})],
    )


def resolver():
    return Resolver(NetBirdClient())


@responses.activate
def test_promotes_preprovisioned_group_member_without_login_or_post():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user()], status=200)
    responses.put("https://nb.test/api/users/u1", json={"id": "u1"}, status=200)

    status = role_binding.sync(parent(), resolver())

    assert status["matchedUsers"] == 1
    assert status["promotedUsers"] == 1
    assert status["restoredUsers"] == 0
    assert status["skippedUsers"] == 0
    assert status["conflicts"] == []
    assert status["managedUsers"] == [
        {"userId": "u1", "previousRole": "user", "appliedRole": "admin"}
    ]
    assert not any(call.request.method == "POST" for call in responses.calls)
    put = next(call for call in responses.calls if call.request.method == "PUT")
    assert json.loads(put.request.body) == {
        "role": "admin",
        "auto_groups": ["g-admin"],
        "is_blocked": False,
    }


@responses.activate
def test_empty_complete_snapshot_is_waited_for_and_never_posted():
    stub_group()
    responses.get("https://nb.test/api/users", json=[], status=200)
    status = role_binding.sync(parent(), resolver())
    assert status["matchedUsers"] == 0
    assert status["managedUsers"] == []
    assert "complete identity snapshot" in status["conditions"][0]["message"]
    assert not any(call.request.method == "POST" for call in responses.calls)


@responses.activate
def test_owner_is_never_modified():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user(role="owner")], status=200)
    status = role_binding.sync(parent(), resolver())
    assert status["managedUsers"] == []
    assert status["skippedUsers"] == 1
    assert "Protected Owner identities skipped" in status["conditions"][0]["message"]
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_operator_owner_never_participates_even_when_service_users_are_included():
    stub_group()
    responses.get(
        "https://nb.test/api/users",
        json=[user(role="user", service=True, name="operator-owner")],
        status=200,
    )
    status = role_binding.sync(parent(includeServiceUsers=True), resolver())
    assert status["managedUsers"] == []
    assert status["skippedUsers"] == 1
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_service_user_is_excluded_by_default():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user(service=True)], status=200)
    status = role_binding.sync(parent(), resolver())
    assert status["managedUsers"] == []
    assert status["skippedUsers"] == 1
    assert "Service users excluded" in status["conditions"][0]["message"]
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_preexisting_admin_is_not_owned():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user(role="admin")], status=200)
    status = role_binding.sync(parent(), resolver())
    assert status["matchedUsers"] == 1
    assert status["managedUsers"] == []
    assert status["skippedUsers"] == 1
    assert not any(call.request.method == "PUT" for call in responses.calls)


def managed_status():
    return {"managedUsers": [{"userId": "u1", "previousRole": "user", "appliedRole": "admin"}]}


@responses.activate
def test_exit_from_group_restores_owned_role():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user(role="admin", groups=[])], status=200)
    responses.put("https://nb.test/api/users/u1", json={"id": "u1"}, status=200)
    status = role_binding.sync(parent(status=managed_status()), resolver())
    assert status["managedUsers"] == []
    assert status["restoredUsers"] == 1
    assert json.loads(responses.calls[-1].request.body)["role"] == "user"


@responses.activate
def test_already_restored_role_on_group_exit_releases_ownership_without_write():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user(role="user", groups=[])], status=200)
    status = role_binding.sync(parent(status=managed_status()), resolver())
    assert status["managedUsers"] == []
    assert status["restoredUsers"] == 1
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_owned_member_role_drift_is_repaired_on_next_cycle():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user(role="user")], status=200)
    responses.put("https://nb.test/api/users/u1", json={"id": "u1"}, status=200)
    status = role_binding.sync(parent(status=managed_status()), resolver())
    assert status["managedUsers"] == managed_status()["managedUsers"]
    assert status["promotedUsers"] == 1
    assert json.loads(responses.calls[-1].request.body)["role"] == "admin"


@responses.activate
def test_delete_restores_only_owned_assignment():
    responses.get("https://nb.test/api/users", json=[user(role="admin", groups=[])], status=200)
    responses.put("https://nb.test/api/users/u1", json={"id": "u1"}, status=200)
    status = role_binding.finalize(parent(status=managed_status()), resolver())
    assert status["managedUsers"] == []
    assert json.loads(responses.calls[-1].request.body)["role"] == "user"


@responses.activate
def test_group_not_found_is_reported_without_user_query():
    stub_group(None)
    status = role_binding.sync(parent(), resolver())
    assert status["synced"] is False
    assert status["conditions"][0]["reason"] == "GroupNotFound"
    assert not any("/api/users" in call.request.url for call in responses.calls)


@responses.activate
def test_incomplete_user_response_causes_no_demotion():
    stub_group()
    responses.get("https://nb.test/api/users", json=[{"id": "u1", "role": "admin"}], status=200)
    status = role_binding.sync(parent(status=managed_status()), resolver())
    assert status["synced"] is False
    assert status["conditions"][0]["reason"] == "IncompleteResponse"
    assert status["managedUsers"] == managed_status()["managedUsers"]
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_incomplete_group_response_causes_no_demotion():
    responses.get(
        "https://nb.test/api/groups", json=[{"name": "vpn-admins"}], status=200,
        match=[responses.matchers.query_param_matcher({"name": "vpn-admins"})],
    )
    status = role_binding.sync(parent(status=managed_status()), resolver())
    assert status["synced"] is False
    assert status["conditions"][0]["reason"] == "IncompleteResponse"
    assert status["managedUsers"] == managed_status()["managedUsers"]
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_conflict_uses_lexical_binding_winner_and_does_not_modify():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user()], status=200)
    other = parent(name="a-binding")
    related = {"NetBirdRoleBinding.netbird.io/v1alpha1": {"a-binding": other}}
    status = role_binding.sync(parent(name="z-binding"), resolver(), related)
    assert status["managedUsers"] == []
    assert len(status["conflicts"]) == 1
    assert "Binding conflicts" in status["conditions"][0]["message"]
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_exit_does_not_demote_when_another_binding_still_matches():
    stub_group()
    responses.get(
        "https://nb.test/api/groups", json=[{"id": "g-other", "name": "other-admins"}],
        status=200, match=[responses.matchers.query_param_matcher({"name": "other-admins"})],
    )
    responses.get(
        "https://nb.test/api/users",
        json=[user(role="admin", groups=["g-other"])], status=200,
    )
    other = parent(name="other", groupRef={"name": "other-admins"})
    related = {"NetBirdRoleBinding.netbird.io/v1alpha1": {"other": other}}
    status = role_binding.sync(parent(status=managed_status()), resolver(), related)
    assert status["managedUsers"] == managed_status()["managedUsers"]
    assert "restoration skipped" in status["conditions"][0]["message"]
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_lexical_winner_deterministically_takes_existing_binding_ownership():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user(role="admin")], status=200)
    previous_owner = parent(name="z-binding", status=managed_status())
    related = {"NetBirdRoleBinding.netbird.io/v1alpha1": {"z-binding": previous_owner}}
    status = role_binding.sync(parent(name="a-binding"), resolver(), related)
    assert status["managedUsers"] == managed_status()["managedUsers"]
    assert "ownership transferred" in status["conflicts"][0]
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_previous_binding_releases_only_after_lexical_winner_owns_role():
    stub_group()
    responses.get("https://nb.test/api/users", json=[user(role="admin")], status=200)
    winner = parent(name="a-binding", status=managed_status())
    related = {"NetBirdRoleBinding.netbird.io/v1alpha1": {"a-binding": winner}}
    status = role_binding.sync(
        parent(name="z-binding", status=managed_status()), resolver(), related
    )
    assert status["managedUsers"] == []
    assert len(status["conflicts"]) == 1
    assert not any(call.request.method == "PUT" for call in responses.calls)


@responses.activate
def test_api_error_conditions_cover_401_404_429_and_5xx():
    client = TestClient(app)
    for code, reason in ((401, "InvalidPAT"), (404, "NetBirdNotFound"), (429, "RateLimited"), (503, "NetBirdUnavailable")):
        responses.reset()
        attempts = 3 if code in (429, 503) else 1
        for _ in range(attempts):
            responses.get("https://nb.test/api/groups", status=code, body="failure")
        result = client.post("/sync/role-binding", json={"parent": parent(status=managed_status())})
        body = result.json()
        assert body["status"]["conditions"][0]["reason"] == reason
        assert body["status"]["managedUsers"] == managed_status()["managedUsers"]
        assert not any(call.request.method in ("PUT", "POST") for call in responses.calls)


@responses.activate
def test_sync_response_requests_configured_periodic_reconciliation():
    stub_group()
    responses.get("https://nb.test/api/users", json=[], status=200)
    result = TestClient(app).post(
        "/sync/role-binding", json={"parent": parent(pollIntervalSeconds=37)}
    )
    assert result.json()["resyncAfterSeconds"] == 37


def test_customize_watches_bindings_cluster_wide():
    result = TestClient(app).post("/customize/role-binding", json={"parent": parent()})
    rule = result.json()["relatedResources"][0]
    assert rule == {
        "apiVersion": "netbird.io/v1alpha1",
        "resource": "netbirdrolebindings",
        "labelSelector": {},
    }


@responses.activate
def test_finalize_api_failure_releases_without_permanent_block():
    responses.get("https://nb.test/api/users", status=401, body="expired")
    result = TestClient(app).post(
        "/finalize/role-binding", json={"parent": parent(status=managed_status())}
    )
    assert result.json()["finalized"] is True
    assert result.json()["status"]["conditions"][0]["reason"] == "RestoreSkipped"
