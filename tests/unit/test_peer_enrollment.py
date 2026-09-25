import base64

from fastapi.testclient import TestClient

from webhook.handlers import peer_enrollment
from webhook.main import app
from webhook.netbird_client import NotFound, Resolver


def parent(*, mode="enroll", status=None):
    spec = {
        "mode": mode,
        "peerIdentity": {"hostname": "cluster-router"},
        "deletionPolicy": "Retain",
    }
    if mode == "enroll":
        spec.update(
            workloadRef={"apiVersion": "apps/v1", "kind": "StatefulSet", "name": "cluster-router"},
            enrollment={
                "secretRef": {"name": "cluster-router-enrollment", "key": "setupKey"},
                "expiresInSeconds": 600,
            },
        )
    else:
        spec["peerIdentity"] = {"id": "p1", "hostname": "cluster-router"}
    return {
        "apiVersion": "netbird.io/v1alpha1",
        "kind": "NetBirdPeerEnrollment",
        "metadata": {
            "name": "cluster-router",
            "namespace": "netbird",
            "uid": "aabbccdd-0011",
            "generation": 1,
        },
        "spec": spec,
        "status": status or {},
    }


def workload_related():
    return {
        "StatefulSet.apps/v1": {
            "cluster-router": {
                "apiVersion": "apps/v1",
                "kind": "StatefulSet",
                "metadata": {"name": "cluster-router", "namespace": "netbird"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "env": [
                                        {
                                            "valueFrom": {
                                                "secretKeyRef": {
                                                    "name": "cluster-router-enrollment",
                                                    "key": "setupKey",
                                                    "optional": True,
                                                }
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                },
            }
        }
    }


class FakeClient:
    def __init__(self, peers=None):
        self.peers = list(peers or [])
        self.setup_keys = []
        self.posts = []
        self.puts = []
        self.deletes = []

    def get(self, path, params=None):
        if path == "/peers":
            return self.peers
        if path == "/setup-keys":
            return self.setup_keys
        if path.startswith("/setup-keys/"):
            return {"id": path.rsplit("/", 1)[1], "state": "valid", "revoked": False}
        if path.startswith("/peers/"):
            peer_id = path.rsplit("/", 1)[1]
            try:
                return next(peer for peer in self.peers if peer["id"] == peer_id)
            except StopIteration as exc:
                raise NotFound(404, "not found", "GET", path) from exc
        raise AssertionError(path)

    def post(self, path, body):
        self.posts.append((path, body))
        return {"id": "sk1", "key": "plain-setup-key"}

    def put(self, path, body):
        self.puts.append((path, body))
        return body

    def delete(self, path):
        self.deletes.append(path)


def peer(peer_id="p1"):
    return {
        "id": peer_id,
        "name": "cluster-router",
        "hostname": "cluster-router",
        "ip": "100.64.0.2",
        "dns_label": "cluster-router.netbird.selfhosted",
        "connected": True,
        "version": "0.58.2",
        "ssh_enabled": False,
        "login_expiration_enabled": False,
        "inactivity_expiration_enabled": False,
    }


def test_enroll_issues_one_off_key_and_temporary_secret():
    client = FakeClient()
    status, children = peer_enrollment.sync_with_children(
        parent(), Resolver(client), {}, workload_related()
    )

    assert status["phase"] == "Enrolling"
    assert status["setupKeyId"] == "sk1"
    assert client.posts == [
        (
            "/setup-keys",
            {
                "name": "runik-enroll-aabbccdd-0011",
                "type": "one-off",
                "expires_in": 600,
                "usage_limit": 1,
                "auto_groups": [],
                "ephemeral": False,
                "allow_extra_dns_labels": False,
            },
        )
    ]
    assert children[0]["metadata"]["name"] == "cluster-router-enrollment"
    assert base64.b64decode(children[0]["data"]["setupKey"]) == b"plain-setup-key"


def test_enroll_persists_peer_id_revokes_key_and_withdraws_secret():
    client = FakeClient(peers=[peer()])
    old = {
        "phase": "Enrolling",
        "setupKeyId": "sk1",
        "setupKeyName": "runik-enroll-aabbccdd-0011",
        "setupKeyRevoked": False,
        "baselinePeerIds": [],
    }
    status, children = peer_enrollment.sync_with_children(
        parent(status=old), Resolver(client), {}, workload_related()
    )

    assert status["phase"] == "Ready"
    assert status["peerId"] == "p1"
    assert status["peerOwned"] is True
    assert status["setupKeyRevoked"] is True
    assert status["wireGuardProfile"]["ip"] == "100.64.0.2"
    assert children == []
    assert ("/setup-keys/sk1", {"auto_groups": [], "revoked": True}) in client.puts


def test_enroll_revokes_key_if_workload_disappears():
    client = FakeClient()
    old = {
        "phase": "Enrolling",
        "setupKeyId": "sk1",
        "setupKeyGroupIds": [],
        "setupKeyRevoked": False,
    }
    status, children = peer_enrollment.sync_with_children(
        parent(status=old), Resolver(client), {}, {}
    )

    assert status["phase"] == "Degraded"
    assert status["conditions"][0]["reason"] == "WorkloadLost"
    assert status["setupKeyRevoked"] is True
    assert children == []
    assert client.puts == [("/setup-keys/sk1", {"auto_groups": [], "revoked": True})]


def test_missing_owned_peer_requires_explicit_replacement_revision():
    client = FakeClient()
    resource = parent(
        status={
            "peerId": "missing",
            "peerOwned": True,
            "observedReplacementRevision": 0,
        }
    )
    status, _ = peer_enrollment.sync_with_children(
        resource, Resolver(client), {}, workload_related()
    )
    assert status["conditions"][0]["reason"] == "PeerMissing"
    assert client.posts == []

    resource["spec"]["enrollment"]["replacementRevision"] = 1
    status, children = peer_enrollment.sync_with_children(
        resource, Resolver(client), {}, workload_related()
    )
    assert status["phase"] == "Enrolling"
    assert status["observedReplacementRevision"] == 1
    assert len(children) == 1


def test_adopt_uses_stable_id_and_reports_duplicate_hostname():
    first = peer("p1")
    second = {**peer("p2"), "dns_label": "cluster-router-1.netbird.selfhosted"}
    client = FakeClient(peers=[first, second])

    status, children = peer_enrollment.sync_with_children(
        parent(mode="adopt"), Resolver(client), {}, {}
    )

    assert status["phase"] == "Conflict"
    assert status["conditions"][0]["reason"] == "DuplicateHostname"
    assert children == []
    assert client.posts == []


def test_finalize_does_not_unregister_retained_adopted_peer():
    client = FakeClient(peers=[peer()])
    resource = parent(mode="adopt", status={"peerId": "p1", "peerOwned": False})

    assert peer_enrollment.finalize(resource, Resolver(client)) == {}
    assert client.deletes == []


def test_customize_observes_only_same_namespace_enroll_workload():
    result = TestClient(app).post("/customize/peer-enrollment", json={"parent": parent()}).json()
    assert result == {
        "relatedResources": [
            {
                "apiVersion": "apps/v1",
                "resource": "statefulsets",
                "namespace": "netbird",
                "names": ["cluster-router"],
            }
        ]
    }
    result = TestClient(app).post(
        "/customize/peer-enrollment", json={"parent": parent(mode="adopt")}
    ).json()
    assert result == {"relatedResources": []}
