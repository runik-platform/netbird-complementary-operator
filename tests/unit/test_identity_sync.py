import base64
import json

import pytest
import responses
from fastapi.testclient import TestClient

from webhook.handlers import identity_sync
from webhook import main as main_module
from webhook.keycloak_client import KeycloakClient, KeycloakError
from webhook.main import app
from webhook.netbird_client import NetBirdError


def parent(*, status=None, **spec_overrides):
    spec = {
        "source": "keycloak-example",
        "instanceId": "default",
        "provider": {
            "baseUrl": "https://sso.test",
            "realm": "example",
            "clientId": "netbird-sync",
            "credentialsSecretRef": {"name": "keycloak-client", "key": "client-secret"},
        },
        "realmRoles": ["operators", "users"],
        "adoptExistingUsers": False,
        "adoptExistingJWTGroups": False,
        "pollIntervalSeconds": 37,
        "deletionPolicy": "Retain",
    }
    spec.update(spec_overrides)
    return {
        "apiVersion": "netbird.io/v1alpha1",
        "kind": "NetBirdIdentitySync",
        "metadata": {
            "name": "keycloak-example",
            "namespace": "netbird",
            "uid": "identity-uid",
            "generation": 2,
        },
        "spec": spec,
        "status": status or {},
    }


def related_secret(value="provider-secret"):
    return {
        "Secret.v1": {
            "keycloak-client": {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": "keycloak-client", "namespace": "netbird"},
                "data": {"client-secret": base64.b64encode(value.encode()).decode()},
            }
        }
    }


class FakeKeycloak:
    def __init__(self, base_url, realm, client_id, client_secret):
        assert (base_url, realm, client_id, client_secret) == (
            "https://sso.test",
            "example",
            "netbird-sync",
            "provider-secret",
        )
        self.issuer = "https://sso.test/realms/example"

    def realm_role(self, name):
        return {"id": f"role-{name}", "name": name}

    def realm_role_users(self, name):
        if name == "operators":
            return [
                {
                    "id": "user-1",
                    "username": "alice",
                    "email": "alice@example.test",
                    "firstName": "Alice",
                    "lastName": "Example",
                    "enabled": True,
                }
            ]
        return [
            {
                "id": "user-1",
                "username": "alice",
                "email": "alice@example.test",
                "firstName": "Alice",
                "lastName": "Example",
                "enabled": True,
            },
            {"id": "user-2", "username": "bob", "enabled": True},
        ]


class FakeNetBird:
    def __init__(self, error=None):
        self.error = error
        self.posts = []

    def post(self, path, body):
        self.posts.append((path, body))
        if self.error:
            raise self.error
        user_results = [
            {
                "external_id": f"{user['issuer']}#{user['subject']}",
                "internal_id": user["subject"],
                "action": "created",
            }
            for user in body["users"]
        ]
        group_results = [
            {
                "external_id": group["external_id"],
                "internal_id": f"internal-{group['external_id']}",
                "action": "created",
            }
            for group in body["groups"]
        ]
        memberships = [
            {**item, "action": "updated"}
            for item in user_results
        ]
        return {
            "source": body["source"],
            "revision": body["revision"],
            "changed": bool(user_results or group_results),
            "users": user_results,
            "groups": group_results,
            "memberships": memberships,
        }


@responses.activate
def test_keycloak_client_authenticates_and_reads_every_role_user_page():
    responses.post(
        "https://sso.test/realms/example/protocol/openid-connect/token",
        json={"access_token": "access-token"},
    )
    responses.get(
        "https://sso.test/admin/realms/example/roles/operators/users",
        json=[{"id": "u1"}, {"id": "u2"}],
        match=[responses.matchers.query_param_matcher({"briefRepresentation": "true", "first": "0", "max": "2"})],
    )
    responses.get(
        "https://sso.test/admin/realms/example/roles/operators/users",
        json=[{"id": "u3"}],
        match=[responses.matchers.query_param_matcher({"briefRepresentation": "true", "first": "2", "max": "2"})],
    )
    responses.get(
        "https://sso.test/admin/realms/example/roles/operators/groups",
        json=[],
        match=[responses.matchers.query_param_matcher({"briefRepresentation": "true", "first": "0", "max": "2"})],
    )

    users = KeycloakClient(
        "https://sso.test", "example", "client", "provider-secret", page_size=2
    ).realm_role_users("operators")

    assert [item["id"] for item in users] == ["u1", "u2", "u3"]
    assert responses.calls[1].request.headers["Authorization"] == "Bearer access-token"
    assert "provider-secret" not in repr(users)


@responses.activate
def test_keycloak_client_includes_role_users_in_groups_and_subgroups():
    responses.post(
        "https://sso.test/realms/example/protocol/openid-connect/token",
        json={"access_token": "access-token"},
    )
    query = responses.matchers.query_param_matcher(
        {"briefRepresentation": "true", "first": "0", "max": "100"}
    )
    responses.get(
        "https://sso.test/admin/realms/example/roles/operators/users",
        json=[{"id": "direct"}, {"id": "shared"}],
        match=[query],
    )
    responses.get(
        "https://sso.test/admin/realms/example/roles/operators/groups",
        json=[{"id": "root-group"}],
        match=[query],
    )
    responses.get(
        "https://sso.test/admin/realms/example/groups/root-group/members",
        json=[{"id": "group-user"}, {"id": "shared"}],
        match=[query],
    )
    responses.get(
        "https://sso.test/admin/realms/example/groups/root-group/children",
        json=[{"id": "child-group"}],
        match=[query],
    )
    responses.get(
        "https://sso.test/admin/realms/example/groups/child-group/members",
        json=[{"id": "child-user"}],
        match=[query],
    )
    responses.get(
        "https://sso.test/admin/realms/example/groups/child-group/children",
        json=[],
        match=[query],
    )

    users = KeycloakClient(
        "https://sso.test", "example", "client", "provider-secret"
    ).realm_role_users("operators")

    assert [item["id"] for item in users] == [
        "direct",
        "shared",
        "group-user",
        "child-user",
    ]


def test_complete_snapshot_is_stable_and_maps_management_ownership_status():
    netbird = FakeNetBird()
    status = identity_sync.sync(
        parent(), netbird, related_secret(), keycloak_factory=FakeKeycloak
    )

    assert status["synced"] is True
    assert status["usersObserved"] == 2
    assert status["groupsObserved"] == 2
    assert status["membershipsApplied"] == 3
    assert [item["external_id"] for item in status["groups"]] == [
        "role-operators",
        "role-users",
    ]
    path, request = netbird.posts[0]
    assert path == "/runik/identity/sync"
    assert request["complete"] is True
    assert request["memberships"][0]["group_external_ids"] == [
        "role-operators",
        "role-users",
    ]
    assert request["revision"] == identity_sync._build_snapshot(parent(), FakeKeycloak(
        "https://sso.test", "example", "netbird-sync", "provider-secret"
    ))[0]["revision"]


def test_provider_failure_preserves_last_known_good_and_never_calls_management():
    previous = {
        "lastCompleteSnapshot": "good-revision",
        "lastSuccessfulSync": "2026-09-15T12:00:00Z",
        "users": [{"external_id": "issuer#u1", "internal_id": "u1", "action": "created"}],
    }

    class BrokenKeycloak(FakeKeycloak):
        def realm_role(self, name):
            raise KeycloakError("ProviderUnavailable", "Keycloak Admin API is unavailable")

    netbird = FakeNetBird()
    status = identity_sync.sync(
        parent(status=previous), netbird, related_secret(), keycloak_factory=BrokenKeycloak
    )

    assert netbird.posts == []
    assert status["lastCompleteSnapshot"] == "good-revision"
    assert status["users"] == previous["users"]
    assert next(c for c in status["conditions"] if c["type"] == "ProviderAvailable")["status"] == "False"


def test_successfully_empty_provider_result_is_applied_as_complete_snapshot():
    class EmptyKeycloak(FakeKeycloak):
        def realm_role_users(self, name):
            return []

    netbird = FakeNetBird()
    status = identity_sync.sync(
        parent(), netbird, related_secret(), keycloak_factory=EmptyKeycloak
    )

    request = netbird.posts[0][1]
    assert request["complete"] is True
    assert request["users"] == []
    assert request["memberships"] == []
    assert status["usersObserved"] == 0
    assert status["conditions"][0]["status"] == "True"


def test_snapshot_conflict_preserves_previous_result_without_provider_secrets():
    previous = {"lastCompleteSnapshot": "good-revision", "usersObserved": 3}
    netbird = FakeNetBird(NetBirdError(409, "provider-secret database detail", "POST", "/runik/identity/sync"))
    status = identity_sync.sync(
        parent(status=previous), netbird, related_secret(), keycloak_factory=FakeKeycloak
    )

    assert status["lastCompleteSnapshot"] == "good-revision"
    assert status["usersObserved"] == 3
    assert status["conditions"][0]["reason"] == "SnapshotConflict"
    assert "provider-secret" not in json.dumps(status)


def test_customize_observes_only_the_named_same_namespace_secret():
    result = TestClient(app).post("/customize/identity-sync", json={"parent": parent()})
    assert result.json() == {
        "relatedResources": [
            {
                "apiVersion": "v1",
                "resource": "secrets",
                "namespace": "netbird",
                "names": ["keycloak-client"],
            }
        ]
    }


def test_sync_hook_uses_the_per_resource_poll_interval(monkeypatch):
    observed = {}

    def fake_sync(parent_object, netbird, related):
        observed["related"] = related
        return {"synced": True, "conditions": []}

    monkeypatch.setattr(main_module, "NetBirdClient", FakeNetBird)
    monkeypatch.setattr(identity_sync, "sync", fake_sync)
    result = TestClient(app).post(
        "/sync/identity-sync",
        json={"parent": parent(), "related": related_secret()},
    )
    assert result.json()["resyncAfterSeconds"] == 37
    assert observed["related"] == related_secret()


def test_retain_finalization_does_not_mutate_management():
    netbird = FakeNetBird()
    status, finalized = identity_sync.finalize(parent(), netbird)
    assert finalized is True
    assert netbird.posts == []
    assert status["conditions"][0]["reason"] == "Retained"


def test_disable_finalization_retries_until_empty_snapshot_is_accepted():
    netbird = FakeNetBird(NetBirdError(503, "unavailable", "POST", "/runik/identity/sync"))
    status, finalized = identity_sync.finalize(
        parent(deletionPolicy="Disable"), netbird
    )
    assert finalized is False
    assert netbird.posts[0][1]["users"] == []
    assert status["conditions"][0]["reason"] == "ManagementUnavailable"


def test_duplicate_provider_role_id_invalidates_snapshot_before_management():
    class DuplicateRoleID(FakeKeycloak):
        def realm_role(self, name):
            return {"id": "same-id", "name": name}

    netbird = FakeNetBird()
    status = identity_sync.sync(
        parent(), netbird, related_secret(), keycloak_factory=DuplicateRoleID
    )
    assert netbird.posts == []
    assert next(c for c in status["conditions"] if c["type"] == "SnapshotValid")["status"] == "False"
