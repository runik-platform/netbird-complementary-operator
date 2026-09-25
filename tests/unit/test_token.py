import base64
import datetime as dt
import hashlib

import responses
from fastapi.testclient import TestClient

from webhook.handlers import token
from webhook.main import app
from webhook.netbird_client import NetBirdClient, Resolver

SPEC = {
    "userRef": "Deploy Bot",
    "name": "CI Token",
    "expiresInDays": 30,
    "renewBeforeDays": 7,
    "secretRef": {"name": "netbird-ci-token", "key": "token"},
}


def _stub_user_lookup():
    responses.get(
        "https://nb.test/api/users",
        json=[{"id": "u1", "name": "Deploy Bot", "email": "bot@example"}],
        status=200,
    )


@responses.activate
def test_creates_token_and_emits_secret():
    _stub_user_lookup()
    responses.post(
        "https://nb.test/api/users/u1/tokens",
        json={
            "plain_token": "nbt_plain_abc123",
            "personal_access_token": {
                "id": "t1",
                "name": "CI Token",
                "expiration_date": "2027-01-01T00:00:00Z",
                "created_by": "u1",
                "created_at": "2026-04-20T00:00:00Z",
            },
        },
        status=200,
    )
    parent = {"spec": SPEC, "status": {}}
    status, children = token.sync_with_children(parent, Resolver(NetBirdClient()), None)
    assert status["id"] == "t1"
    assert status["userId"] == "u1"
    assert len(children) == 1
    sec = children[0]
    assert sec["kind"] == "Secret"
    assert sec["metadata"]["name"] == "netbird-ci-token"
    assert sec["data"]["token"] == base64.b64encode(b"nbt_plain_abc123").decode()


@responses.activate
def test_preserves_existing_secret_when_token_still_valid():
    _stub_user_lookup()
    responses.get(
        "https://nb.test/api/users/u1/tokens/t1",
        json={"id": "t1", "expiration_date": "2027-01-01T00:00:00Z"},
        status=200,
    )
    existing_secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "netbird-ci-token"},
        "data": {"token": "b2xkdG9rZW4="},
    }
    children = {"Secret.v1": {"netbird-ci-token": existing_secret}}
    parent = {"spec": SPEC, "status": {"id": "t1"}}
    status, returned = token.sync_with_children(parent, Resolver(NetBirdClient()), children)
    assert status["id"] == "t1"
    assert returned == [existing_secret]
    assert not any(c.request.method == "POST" for c in responses.calls)


@responses.activate
def test_finalize_deletes_token_upstream():
    responses.delete("https://nb.test/api/users/u1/tokens/t1", status=200)
    parent = {"spec": SPEC, "status": {"id": "t1", "userId": "u1"}}
    assert token.finalize(parent, Resolver(NetBirdClient())) == {}


@responses.activate
def test_finalize_tolerates_missing_user():
    responses.get("https://nb.test/api/users", status=401, body='{"message":"token invalid"}')
    parent = {"spec": SPEC, "status": {}}
    assert token.finalize(parent, Resolver(NetBirdClient())) == {}


@responses.activate
def test_rotates_before_expiry_without_deleting_old_token(monkeypatch):
    monkeypatch.setattr(token, "_now", lambda: dt.datetime(2026, 8, 23, tzinfo=dt.timezone.utc))
    _stub_user_lookup()
    responses.get(
        "https://nb.test/api/users/u1/tokens/t1",
        json={"id": "t1", "expiration_date": "2026-08-25T00:00:00Z"},
        status=200,
    )
    responses.post(
        "https://nb.test/api/users/u1/tokens",
        json={
            "plain_token": "new-plaintext",
            "personal_access_token": {"id": "t2", "expiration_date": "2026-09-22T00:00:00Z"},
        },
        status=200,
    )
    existing = {
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "netbird-ci-token"},
        "data": {"token": base64.b64encode(b"old-plaintext").decode()},
    }
    status, desired = token.sync_with_children(
        {"spec": SPEC, "status": {"id": "t1"}}, Resolver(NetBirdClient()),
        {"Secret.v1": {"netbird-ci-token": existing}},
    )
    assert status["activeTokenId"] == "t2"
    assert status["previousTokenId"] == "t1"
    assert desired[0]["data"]["token"] == base64.b64encode(b"new-plaintext").decode()
    assert not any(call.request.method == "DELETE" for call in responses.calls)


@responses.activate
def test_old_token_is_allowed_to_expire_after_new_secret_is_observed(monkeypatch):
    monkeypatch.setattr(token, "_now", lambda: dt.datetime(2026, 8, 23, tzinfo=dt.timezone.utc))
    _stub_user_lookup()
    responses.get(
        "https://nb.test/api/users/u1/tokens/t2",
        json={"id": "t2", "expiration_date": "2026-09-22T00:00:00Z"}, status=200,
    )
    encoded = base64.b64encode(b"new-plaintext").decode()
    existing = {
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "netbird-ci-token"},
        "data": {"token": encoded},
    }
    old_status = {
        "id": "t2", "activeTokenId": "t2", "previousTokenId": "t1",
        "pendingSecretHash": hashlib.sha256(encoded.encode()).hexdigest(),
        "lastRotationTime": "2026-08-23T00:00:00Z",
    }
    status, desired = token.sync_with_children(
        {"spec": SPEC, "status": old_status}, Resolver(NetBirdClient()),
        {"Secret.v1": {"netbird-ci-token": existing}},
    )
    assert "previousTokenId" not in status
    assert "pendingSecretHash" not in status
    assert desired == [existing]
    assert not any(call.request.method == "DELETE" for call in responses.calls)


@responses.activate
def test_expired_token_reports_break_glass_and_does_not_rotate(monkeypatch):
    monkeypatch.setattr(token, "_now", lambda: dt.datetime(2026, 8, 23, tzinfo=dt.timezone.utc))
    _stub_user_lookup()
    responses.get(
        "https://nb.test/api/users/u1/tokens/t1",
        json={"id": "t1", "expiration_date": "2026-08-22T00:00:00Z"}, status=200,
    )
    existing = {
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "netbird-ci-token"},
        "data": {"token": base64.b64encode(b"expired").decode()},
    }
    status, desired = token.sync_with_children(
        {"spec": SPEC, "status": {"id": "t1"}}, Resolver(NetBirdClient()),
        {"Secret.v1": {"netbird-ci-token": existing}},
    )
    assert status["synced"] is False
    assert status["conditions"][0]["reason"] == "TokenExpired"
    assert status["conditions"][2]["reason"] == "BootstrapRequired"
    assert desired == [existing]
    assert not any(call.request.method == "POST" for call in responses.calls)


@responses.activate
def test_401_for_known_expired_bootstrap_pat_preserves_secret():
    responses.get("https://nb.test/api/users", status=401, body="token invalid")
    existing = {
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "netbird-ci-token"},
        "data": {"token": base64.b64encode(b"expired").decode()},
    }
    parent = {
        "spec": SPEC,
        "status": {"id": "t1", "expiresAt": "2020-01-01T00:00:00Z"},
    }
    result = TestClient(app).post(
        "/sync/token",
        json={"parent": parent, "children": {"Secret.v1": {"netbird-ci-token": existing}}},
    ).json()
    assert result["status"]["conditions"][0]["reason"] == "BootstrapRequired"
    assert result["children"] == [existing]
