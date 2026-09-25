import responses
from fastapi.testclient import TestClient

from webhook.main import _instance_error_status, app


@responses.activate
def test_success_status_is_stable_across_identical_reconciliations():
    responses.get("https://nb.test/api/networks", json=[])
    client = TestClient(app)
    parent = {
        "metadata": {"name": "gone", "generation": 7},
        "spec": {"name": "gone", "state": "absent"},
    }

    first = client.post("/sync/network", json={"parent": parent}).json()["status"]
    parent["status"] = first
    second = client.post("/sync/network", json={"parent": parent}).json()["status"]

    assert second == first


def test_error_status_is_stable_across_identical_reconciliations():
    client = TestClient(app)
    parent = {
        "metadata": {"name": "invalid", "generation": 3},
        "spec": {"name": "invalid", "enabled": False},
    }

    first = client.post("/sync/network", json={"parent": parent}).json()["status"]
    parent["status"] = first
    second = client.post("/sync/network", json={"parent": parent}).json()["status"]

    assert second == first


def test_instance_error_never_reopens_a_sealed_bootstrap():
    parent = {
        "metadata": {"name": "default", "generation": 1},
        "status": {"bootstrap": {"phase": "BootstrapSealed"}},
    }

    status = _instance_error_status(parent, "ManagementUnavailable", "restarting")

    assert status["bootstrap"]["phase"] == "BootstrapSealed"


def test_instance_error_preserves_credential_revisions():
    parent = {
        "metadata": {"name": "default", "generation": 2},
        "status": {
            "bootstrap": {"phase": "BootstrapSealed"},
            "credentialRotation": {"observedRevision": "rotate-1"},
            "credentialRecovery": {"observedRevision": "recover-1"},
        },
    }

    status = _instance_error_status(parent, "RecoveryRequired", "restore credential")

    assert status["credentialRotation"] == {"observedRevision": "rotate-1"}
    assert status["credentialRecovery"] == {"observedRevision": "recover-1"}
