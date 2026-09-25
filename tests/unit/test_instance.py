import base64
import json

import pytest

from webhook.handlers import instance


DIGEST = "sha256:" + "1" * 64
PARENT = {
    "apiVersion": "netbird.io/v1alpha1",
    "kind": "NetBirdInstance",
    "metadata": {
        "name": "default",
        "namespace": "netbird",
        "uid": "instance-uid",
        "generation": 1,
    },
    "spec": {
        "management": {
            "mode": "managed",
            "image": {"repository": "registry/netbird-fork", "digest": DIGEST},
            "publicEndpoint": "https://vpn.example.com",
        },
        "dashboard": {
            "mode": "managed",
            "image": {"repository": "netbirdio/dashboard", "tag": "v2.92.0"},
        },
        "signals": [
            {
                "name": "primary",
                "location": "cluster",
                "mode": "managed",
                "image": {"repository": "netbirdio/signal", "tag": "0.78.2"},
            }
        ],
        "relays": [
            {
                "name": "primary",
                "location": "cluster",
                "mode": "managed",
                "image": {"repository": "netbirdio/relay", "tag": "0.78.2"},
            }
        ],
        "stunTurns": [
            {
                "name": "primary",
                "location": "cluster",
                "mode": "external",
                "endpoints": ["stun:stun.example.com:3478"],
            }
        ],
        "database": {"secretRef": {"name": "postgres", "key": "uri"}},
        "encryptionKeySecretRef": {"name": "encryption", "key": "key"},
        "oidc": {
            "issuer": "https://sso.example.com/realms/netbird",
            "clientID": "netbird",
            "clientSecretRef": {"name": "oidc", "key": "secret"},
            "redirectURLs": ["https://vpn.example.com/auth/callback"],
        },
        "bootstrap": {
            "accountName": "example",
            "ownerName": "operator-owner",
            "credentialSecretRef": {"name": "netbird-mgmt-api-key", "key": "NB_API_KEY"},
        },
        "lifecycle": {"deletionPolicy": "Retain"},
    },
}


def _by_kind(children):
    return {(item["kind"], item["metadata"]["name"]): item for item in children}


def _observed_children(desired):
    result = {}
    for item in desired:
        key = f"{item['kind']}.apps/v1" if item["apiVersion"] == "apps/v1" else f"{item['kind']}.v1"
        observed = json.loads(json.dumps(item))
        if item["kind"] == "Deployment" and item["metadata"]["name"] == instance.MANAGEMENT_NAME:
            observed["status"] = {"availableReplicas": 1}
        result.setdefault(key, {})[item["metadata"]["name"]] = observed
    return result


class FakeBootstrapClient:
    def __init__(self, phase="pending", fail_ack=False, active_pat="nbp_owner"):
        self.phase = phase
        self.fail_ack = fail_ack
        self.active_pat = active_pat
        self.bootstrap_calls = 0
        self.adopt_calls = 0
        self.ack_calls = 0
        self.verify_calls = 0
        self.prepare_calls = 0
        self.complete_calls = 0
        self.adoption = None
        self.account_id = "runik-account"
        self.pending_revision = None
        self.pending_pat = None

    def status(self, _instance_id):
        return {
            "phase": self.phase,
            "account_id": "runik-account",
            "owner_user_id": "runik-owner",
        }

    def bootstrap(self, _token, payload):
        self.bootstrap_calls += 1
        return {
            "phase": "bootstrapping",
            "account_id": payload["account_id"],
            "owner_user_id": "runik-owner",
            "personal_access_token": "nbp_owner",
        }

    def adopt(self, pat, bootstrap_secret, payload):
        self.adopt_calls += 1
        self.adoption = (pat, bootstrap_secret, payload)
        self.account_id = "existing-account"
        return {
            "phase": "bootstrapping",
            "account_id": "existing-account",
            "owner_user_id": "runik-owner",
            "personal_access_token": "nbp_owner",
        }

    def ack(self, _instance_id, pat):
        self.ack_calls += 1
        if self.fail_ack:
            raise instance.InstanceError("BootstrapAPIError", "rejected")
        assert pat == "nbp_owner"
        self.phase = "sealed"
        return {"phase": "sealed", "account_id": self.account_id, "owner_user_id": "runik-owner"}

    def verify_credential(self, _instance_id, pat):
        self.verify_calls += 1
        if pat != self.active_pat:
            raise instance.InstanceError("BootstrapAPIError", "inactive credential")
        return {"phase": "sealed", "account_id": self.account_id, "owner_user_id": "runik-owner"}

    def prepare_credential_rotation(self, _instance_id, revision, pat):
        self.prepare_calls += 1
        if pat != self.active_pat:
            raise instance.InstanceError("BootstrapAPIError", "inactive credential")
        self.pending_revision = revision
        self.pending_pat = f"nbp_rotated_{revision}"
        return {
            "phase": "rotating",
            "account_id": self.account_id,
            "owner_user_id": "runik-owner",
            "personal_access_token": self.pending_pat,
        }

    def complete_credential_rotation(self, _instance_id, revision, pat):
        self.complete_calls += 1
        if revision != self.pending_revision or pat != self.pending_pat:
            raise instance.InstanceError("BootstrapAPIError", "invalid replacement")
        self.active_pat = pat
        self.pending_revision = None
        self.pending_pat = None
        return {"phase": "sealed", "account_id": self.account_id, "owner_user_id": "runik-owner"}


class FakeSecretStore:
    def __init__(self, secret=None):
        self.secret = secret
        self.create_calls = 0
        self.update_calls = 0

    def get(self, _namespace, _name):
        return self.secret

    def create(self, namespace, name, key, value):
        self.create_calls += 1
        self.secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"namespace": namespace, "name": name},
            "data": {key: base64.b64encode(value.encode()).decode()},
        }
        return self.secret

    def update(self, namespace, name, key, value, annotations):
        self.update_calls += 1
        self.secret = json.loads(json.dumps(self.secret))
        self.secret.setdefault("metadata", {}).update(
            {"namespace": namespace, "name": name, "annotations": annotations}
        )
        self.secret.setdefault("data", {})[key] = base64.b64encode(value.encode()).decode()
        return self.secret


class MappingSecretStore(FakeSecretStore):
    def __init__(self, secrets):
        super().__init__()
        self.secrets = secrets

    def get(self, _namespace, name):
        return self.secrets.get(name)

    def create(self, namespace, name, key, value):
        secret = super().create(namespace, name, key, value)
        self.secrets[name] = secret
        return secret

    def update(self, namespace, name, key, value, annotations):
        self.secret = self.secrets[name]
        secret = super().update(namespace, name, key, value, annotations)
        self.secrets[name] = secret
        return secret


class FakeOwnershipStore:
    def __init__(self):
        self.calls = []

    def orphan(self, namespace, parent_uid, children):
        self.calls.append((namespace, parent_uid, children))


def test_first_sync_renders_runtime_without_requiring_pat():
    status, desired, resync = instance.sync_with_children(PARENT, {})
    objects = _by_kind(desired)
    assert status["phase"] == "Progressing"
    assert status["bootstrap"]["phase"] == "BootstrapPending"
    assert resync == 10
    assert ("Secret", instance.BOOTSTRAP_SECRET_NAME) in objects
    assert ("Deployment", instance.MANAGEMENT_NAME) in objects
    assert objects[("Service", instance.MANAGEMENT_NAME)]["spec"]["ports"] == [
        {
            "name": "http2-management",
            "appProtocol": "http2",
            "port": instance.MANAGEMENT_PORT,
            "targetPort": instance.MANAGEMENT_PORT,
            "protocol": "TCP",
        }
    ]
    assert ("Deployment", "netbird-dashboard") not in objects
    assert ("Deployment", "netbird-signal-primary") not in objects
    assert ("Deployment", "netbird-relay-primary") not in objects
    management = objects[("Deployment", instance.MANAGEMENT_NAME)]
    assert management["spec"]["template"]["spec"]["containers"][0]["image"] == f"registry/netbird-fork@{DIGEST}"
    config = json.loads(objects[("ConfigMap", "netbird-management-config")]["data"]["management.json"])
    assert config["DataStoreEncryptionKey"] == "{{.DATA_STORE_ENCRYPTION_KEY}}"
    assert config["Relay"]["Secret"] == "{{.NB_RELAY_SECRET}}"
    assert config["PKCEAuthorizationFlow"]["ProviderConfig"]["ClientSecret"] == "{{.OIDC_CLIENT_SECRET}}"
    assert config["Signal"]["URI"] == "netbird-signal-primary:33000"
    assert "nbp_" not in json.dumps(desired)


def test_management_revision_rolls_before_dependent_components():
    _, initial, _ = instance.sync_with_children(PARENT, {})
    observed = _observed_children(initial)
    old_dashboard = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "netbird-dashboard"},
        "spec": {"template": {"spec": {"containers": [{"image": "old-dashboard"}]}}},
    }
    observed.setdefault("Deployment.apps/v1", {})["netbird-dashboard"] = old_dashboard
    parent = json.loads(json.dumps(PARENT))
    parent["spec"]["oidc"]["issuer"] = "https://sso-new.example.com/realms/netbird"
    status, returned, _ = instance.sync_with_children(parent, observed)
    objects = _by_kind(returned)
    assert status["phase"] == "Progressing"
    assert objects[("Deployment", "netbird-dashboard")] == old_dashboard


def test_bootstrap_persists_owner_secret_then_seals_and_removes_channel():
    _, initial, _ = instance.sync_with_children(PARENT, {})
    observed = _observed_children(initial)
    client = FakeBootstrapClient()
    secrets_store = FakeSecretStore()

    status, desired, resync = instance.sync_with_children(
        PARENT, observed, bootstrap_client=client, secret_store=secrets_store
    )

    assert client.bootstrap_calls == 1
    assert client.ack_calls == 1
    assert secrets_store.create_calls == 1
    assert status["bootstrap"]["phase"] == "BootstrapSealed"
    assert status["phase"] == "Progressing"
    assert status["migrations"]["phase"] == "Succeeded"
    assert next(c for c in status["conditions"] if c["type"] == "RuntimeReady")["status"] == "True"
    assert next(c for c in status["conditions"] if c["type"] == "Ready")["status"] == "False"
    assert resync == 10
    objects = _by_kind(desired)
    assert ("Secret", instance.BOOTSTRAP_SECRET_NAME) not in objects
    management = objects[("Deployment", instance.MANAGEMENT_NAME)]
    env_names = {item["name"] for item in management["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert "NB_RUNIK_BOOTSTRAP_SECRET_FILE" not in env_names
    assert "nbp_owner" not in json.dumps(status)


def test_existing_account_adoption_uses_owner_pat_and_bootstrap_secret():
    parent = json.loads(json.dumps(PARENT))
    parent["spec"]["bootstrap"]["adoptionCredentialSecretRef"] = {
        "name": "existing-owner",
        "key": "NB_API_KEY",
    }
    _, initial, _ = instance.sync_with_children(parent, {})
    observed = _observed_children(initial)
    store = MappingSecretStore(
        {
            "existing-owner": {
                "data": {"NB_API_KEY": base64.b64encode(b"nbp_existing_owner").decode()}
            }
        }
    )
    client = FakeBootstrapClient()

    status, _, _ = instance.sync_with_children(
        parent, observed, bootstrap_client=client, secret_store=store
    )

    bootstrap_secret = base64.b64decode(
        observed["Secret.v1"][instance.BOOTSTRAP_SECRET_NAME]["data"][
            instance.BOOTSTRAP_SECRET_KEY
        ]
    ).decode()
    assert client.bootstrap_calls == 0
    assert client.adopt_calls == 1
    assert client.adoption == (
        "nbp_existing_owner",
        bootstrap_secret,
        {"instance_id": "netbird/default", "account_name": "example"},
    )
    assert client.ack_calls == 1
    assert store.create_calls == 1
    assert status["accountID"] == "existing-account"


def test_public_oidc_and_managed_component_endpoints_need_no_client_secret():
    parent = json.loads(json.dumps(PARENT))
    del parent["spec"]["oidc"]["clientSecretRef"]
    parent["spec"]["signals"][0]["publicEndpoint"] = "grpc://vpn.example.com:33000"
    parent["spec"]["relays"][0]["publicEndpoint"] = "rel://vpn.example.com:33080"

    _, desired, _ = instance.sync_with_children(parent, {})
    objects = _by_kind(desired)
    config = json.loads(
        objects[("ConfigMap", "netbird-management-config")]["data"]["management.json"]
    )
    assert config["Signal"] == {"Proto": "http", "URI": "vpn.example.com:33000"}
    assert config["Relay"]["Addresses"] == ["rel://vpn.example.com:33080"]
    assert "ClientSecret" not in config["PKCEAuthorizationFlow"]["ProviderConfig"]
    management = objects[("Deployment", instance.MANAGEMENT_NAME)]
    env_names = {
        item["name"]
        for item in management["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert "OIDC_CLIENT_SECRET" not in env_names


def test_oidc_defaults_support_dashboard_and_local_client_login():
    parent = json.loads(json.dumps(PARENT))
    del parent["spec"]["oidc"]["redirectURLs"]

    desired, _ = instance._runtime_children(parent, {}, False)
    objects = _by_kind(desired)
    config = json.loads(
        objects[("ConfigMap", "netbird-management-config")]["data"]["management.json"]
    )
    provider = config["PKCEAuthorizationFlow"]["ProviderConfig"]
    assert provider["Scope"] == instance.OIDC_SCOPES
    assert provider["RedirectURLs"] == instance.DEFAULT_PKCE_REDIRECT_URLS

    dashboard = objects[("Deployment", "netbird-dashboard")]
    env = {
        item["name"]: item["value"]
        for item in dashboard["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["AUTH_SUPPORTED_SCOPES"] == instance.OIDC_SCOPES
    assert env["AUTH_REDIRECT_URI"] == instance.DASHBOARD_REDIRECT_URI
    assert env["AUTH_SILENT_REDIRECT_URI"] == instance.DASHBOARD_SILENT_REDIRECT_URI


def test_restart_after_secret_write_acks_without_requesting_another_pat():
    _, initial, _ = instance.sync_with_children(PARENT, {})
    observed = _observed_children(initial)
    secret = {
        "data": {"NB_API_KEY": base64.b64encode(b"nbp_owner").decode()}
    }
    client = FakeBootstrapClient(phase="bootstrapping")
    status, _, _ = instance.sync_with_children(
        PARENT,
        observed,
        bootstrap_client=client,
        secret_store=FakeSecretStore(secret),
    )
    assert client.bootstrap_calls == 0
    assert client.ack_calls == 1
    assert status["bootstrap"]["phase"] == "BootstrapSealed"


def test_sealed_instance_becomes_ready_after_bootstrap_child_is_gone():
    parent = json.loads(json.dumps(PARENT))
    parent["status"] = {"bootstrap": {"phase": "BootstrapSealed"}}
    desired, _ = instance._runtime_children(parent, {}, False)
    observed = _observed_children(desired)
    status, returned, resync = instance.sync_with_children(
        parent,
        observed,
        bootstrap_client=FakeBootstrapClient(phase="sealed"),
        secret_store=FakeSecretStore(
            {"data": {"NB_API_KEY": base64.b64encode(b"nbp_owner").decode()}}
        ),
    )
    assert status["phase"] == "Ready"
    assert status["synced"] is True
    assert resync == 300
    assert _by_kind(returned).keys() == _by_kind(desired).keys()


def test_sealed_owner_credential_rotates_after_secret_is_persisted():
    parent = json.loads(json.dumps(PARENT))
    parent["spec"]["bootstrap"]["rotationRevision"] = "2026-09-17"
    parent["status"] = {"bootstrap": {"phase": "BootstrapSealed"}}
    desired, _ = instance._runtime_children(parent, {}, False)
    observed = _observed_children(desired)
    client = FakeBootstrapClient(phase="sealed")
    store = FakeSecretStore(
        {"data": {"NB_API_KEY": base64.b64encode(b"nbp_owner").decode()}}
    )

    status, returned, _ = instance.sync_with_children(
        parent, observed, bootstrap_client=client, secret_store=store
    )

    assert client.verify_calls == 1
    assert client.prepare_calls == 1
    assert client.complete_calls == 1
    assert store.update_calls == 1
    assert status["credentialRotation"]["observedRevision"] == "2026-09-17"
    assert "rotatedAt" in status["credentialRotation"]
    assert "nbp_rotated" not in json.dumps(status)
    assert "nbp_rotated" not in json.dumps(returned)


def test_rotation_resumes_after_secret_write_without_preparing_again():
    parent = json.loads(json.dumps(PARENT))
    parent["spec"]["bootstrap"]["rotationRevision"] = "2026-09-17"
    parent["status"] = {"bootstrap": {"phase": "BootstrapSealed"}}
    desired, _ = instance._runtime_children(parent, {}, False)
    observed = _observed_children(desired)
    client = FakeBootstrapClient(phase="sealed")
    client.pending_revision = "2026-09-17"
    client.pending_pat = "nbp_rotated_2026-09-17"
    store = FakeSecretStore(
        {
            "metadata": {
                "annotations": {
                    instance.ROTATION_REVISION_ANNOTATION: "2026-09-17"
                }
            },
            "data": {
                "NB_API_KEY": base64.b64encode(b"nbp_rotated_2026-09-17").decode()
            },
        }
    )

    status, _, _ = instance.sync_with_children(
        parent, observed, bootstrap_client=client, secret_store=store
    )

    assert client.verify_calls == 0
    assert client.prepare_calls == 0
    assert client.complete_calls == 1
    assert store.update_calls == 0
    assert status["credentialRotation"]["observedRevision"] == "2026-09-17"


def test_recovery_requires_a_new_revision_after_restoring_valid_secret():
    parent = json.loads(json.dumps(PARENT))
    parent["status"] = {
        "phase": "Degraded",
        "bootstrap": {"phase": "BootstrapSealed"},
        "credentialRecovery": {"observedRevision": ""},
        "conditions": [{"reason": "RecoveryRequired"}],
    }
    desired, _ = instance._runtime_children(parent, {}, False)
    observed = _observed_children(desired)
    client = FakeBootstrapClient(phase="sealed")
    store = FakeSecretStore(
        {"data": {"NB_API_KEY": base64.b64encode(b"nbp_owner").decode()}}
    )

    with pytest.raises(instance.InstanceError, match="recoveryRevision") as error:
        instance.sync_with_children(
            parent, observed, bootstrap_client=client, secret_store=store
        )
    assert error.value.reason == "RecoveryRequired"

    parent["spec"]["bootstrap"]["recoveryRevision"] = "restore-2026-09-17"
    status, _, _ = instance.sync_with_children(
        parent, observed, bootstrap_client=client, secret_store=store
    )
    assert status["credentialRecovery"]["observedRevision"] == "restore-2026-09-17"
    assert "recoveredAt" in status["credentialRecovery"]


def test_mismatched_owner_secret_requires_explicit_recovery():
    _, initial, _ = instance.sync_with_children(PARENT, {})
    observed = _observed_children(initial)
    secret = {"data": {"NB_API_KEY": base64.b64encode(b"nbp_wrong").decode()}}
    with pytest.raises(instance.InstanceError, match="does not match") as error:
        instance.sync_with_children(
            PARENT,
            observed,
            bootstrap_client=FakeBootstrapClient(phase="bootstrapping", fail_ack=True),
            secret_store=FakeSecretStore(secret),
        )
    assert error.value.reason == "RecoveryRequired"


def test_retain_finalizer_orphans_runtime_before_releasing_parent():
    child = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "netbird-management",
            "ownerReferences": [{"uid": "instance-uid", "controller": True}],
        },
    }
    store = FakeOwnershipStore()
    status, children, finalized = instance.finalize(
        PARENT,
        {"Deployment.apps/v1": {"netbird-management": child}},
        ownership_store=store,
    )
    assert status["retainedRuntime"] is True
    assert children == []
    assert finalized is True
    assert store.calls == [("netbird", "instance-uid", [child])]


def test_retain_finalizer_closes_bootstrap_channel_before_orphaning():
    _, desired, _ = instance.sync_with_children(PARENT, {})
    observed = _observed_children(desired)
    status, returned, finalized = instance.finalize(PARENT, observed)
    objects = _by_kind(returned)
    assert status["retainedRuntime"] is False
    assert finalized is False
    assert ("Secret", instance.BOOTSTRAP_SECRET_NAME) not in objects
    management = objects[("Deployment", instance.MANAGEMENT_NAME)]
    container = management["spec"]["template"]["spec"]["containers"][0]
    assert all(env["name"] != "NB_RUNIK_BOOTSTRAP_SECRET_FILE" for env in container["env"])


def test_delete_runtime_waits_for_children_to_be_deleted():
    parent = json.loads(json.dumps(PARENT))
    parent["spec"]["lifecycle"]["deletionPolicy"] = "DeleteRuntime"
    child = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "netbird-management"}}
    status, children, finalized = instance.finalize(
        parent, {"Service.v1": {"netbird-management": child}}
    )
    assert status["retainedRuntime"] is False
    assert children == []
    assert finalized is False
    assert instance.finalize(parent, {})[2] is True
