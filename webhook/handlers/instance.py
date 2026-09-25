"""NetBirdInstance runtime and sealed-bootstrap reconciler."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import secrets
from typing import Any
from urllib.parse import quote, urlparse

import requests


BOOTSTRAP_SECRET_NAME = "netbird-bootstrap"
BOOTSTRAP_SECRET_KEY = "token"
MANAGEMENT_NAME = "netbird-management"
MANAGEMENT_PORT = 33073
OIDC_SCOPES = "openid profile email groups"
DEFAULT_PKCE_REDIRECT_URLS = ["http://localhost:53000/", "http://localhost:54000/"]
DASHBOARD_REDIRECT_URI = "/auth"
DASHBOARD_SILENT_REDIRECT_URI = "/silent-auth"
ROTATION_REVISION_ANNOTATION = "netbird.io/operator-owner-rotation-revision"


class InstanceError(Exception):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _condition(kind: str, value: str, reason: str, message: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"type": kind, "status": value, "reason": reason}
    if message:
        result["message"] = message[:2000]
    return result


def _status(
    parent: dict[str, Any],
    *,
    phase: str,
    bootstrap_phase: str,
    ready: bool,
    runtime_ready: bool | None = None,
    reason: str,
    message: str = "",
    identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if runtime_ready is None:
        runtime_ready = ready
    status: dict[str, Any] = {
        "phase": phase,
        "synced": ready,
        "bootstrap": {"phase": bootstrap_phase},
        "conditions": [
            _condition("RuntimeReady", "True" if runtime_ready else "False", reason, message),
            _condition(
                "BootstrapReady",
                "True" if bootstrap_phase == "BootstrapSealed" else "False",
                reason,
                message,
            ),
            _condition("Ready", "True" if ready else "False", reason, message),
        ],
    }
    generation = (parent.get("metadata") or {}).get("generation")
    for condition in status["conditions"]:
        condition["observedGeneration"] = generation
    if identity:
        status.update(
            {
                "accountID": identity.get("account_id", ""),
                "ownerUserID": identity.get("owner_user_id", ""),
            }
        )
    status["migrations"] = {"phase": "Succeeded" if runtime_ready else "Pending"}
    spec = parent.get("spec") or {}
    credential_ref = (spec.get("bootstrap") or {}).get("credentialSecretRef")
    if credential_ref:
        status["credentialSecretRef"] = credential_ref
    return status


def _credential_status(
    parent: dict[str, Any],
    status: dict[str, Any],
    *,
    rotation_revision: str,
    recovery_revision: str,
    rotated: bool = False,
    recovered: bool = False,
) -> dict[str, Any]:
    previous = parent.get("status") or {}
    rotation = dict(previous.get("credentialRotation") or {})
    recovery = dict(previous.get("credentialRecovery") or {})
    rotation["observedRevision"] = rotation_revision
    recovery["observedRevision"] = recovery_revision
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    if rotated:
        rotation["rotatedAt"] = now
    if recovered:
        recovery["recoveredAt"] = now
    status["credentialRotation"] = rotation
    status["credentialRecovery"] = recovery
    return status


def _objects(children: dict[str, Any] | None, kind: str) -> dict[str, dict[str, Any]]:
    if not children:
        return {}
    return children.get(f"{kind}.v1") or children.get(f"{kind}.apps/v1") or children.get(f"{kind}.") or {}


def _existing(children: dict[str, Any] | None, kind: str, name: str) -> dict[str, Any] | None:
    return _objects(children, kind).get(name)


def _image(spec: dict[str, Any]) -> str:
    image = spec.get("image") or {}
    repository = image.get("repository")
    if not repository:
        raise InstanceError("InvalidSpec", "managed components require image.repository")
    if image.get("digest"):
        return f"{repository}@{image['digest']}"
    if image.get("tag"):
        return f"{repository}:{image['tag']}"
    raise InstanceError("InvalidSpec", "managed components require image.digest or image.tag")


def _labels(component: str) -> dict[str, str]:
    return {
        "app.kubernetes.io/name": f"netbird-{component}",
        "app.kubernetes.io/part-of": "netbird",
        "app.kubernetes.io/managed-by": "netbird-complementary-operator",
    }


def _secret_child(name: str, key: str, value: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name},
        "type": "Opaque",
        "data": {key: base64.b64encode(value.encode()).decode()},
    }


def _config_child(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "netbird-management-config"},
        "data": {"management.json": json.dumps(config, indent=2, sort_keys=True)},
    }


def _service(
    name: str,
    component: str,
    port: int,
    service_spec: dict[str, Any] | None = None,
    *,
    port_name: str = "service",
    app_protocol: str | None = None,
) -> dict[str, Any]:
    service_spec = service_spec or {}
    labels = _labels(component)
    service_port = {
        "name": port_name,
        "port": port,
        "targetPort": port,
        "protocol": service_spec.get("protocol", "TCP"),
    }
    if app_protocol:
        service_port["appProtocol"] = app_protocol
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "labels": labels, "annotations": service_spec.get("annotations", {})},
        "spec": {
            "type": service_spec.get("type", "ClusterIP"),
            "selector": labels,
            "ports": [service_port],
        },
    }


def _deployment(
    name: str,
    component: str,
    image: str,
    *,
    args: list[str] | None = None,
    env: list[dict[str, Any]] | None = None,
    ports: list[int] | None = None,
    volumes: list[dict[str, Any]] | None = None,
    mounts: list[dict[str, Any]] | None = None,
    revision_data: Any | None = None,
) -> dict[str, Any]:
    labels = _labels(component)
    container: dict[str, Any] = {
        "name": component,
        "image": image,
        "imagePullPolicy": "IfNotPresent",
        "securityContext": {
            "allowPrivilegeEscalation": False,
        },
        "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}},
    }
    if args:
        container["args"] = args
    if env:
        container["env"] = env
    if ports:
        container["ports"] = [{"name": f"p{port}", "containerPort": port} for port in ports]
    if mounts:
        container["volumeMounts"] = mounts
    pod_spec: dict[str, Any] = {
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container],
    }
    if volumes:
        pod_spec["volumes"] = volumes
    revision = hashlib.sha256(
        json.dumps(
            {
                "image": image,
                "args": args,
                "env": env,
                "ports": ports,
                "volumes": volumes,
                "mounts": mounts,
                "config": revision_data,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()[:16]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": labels},
            "template": {
                "metadata": {
                    "labels": labels,
                    "annotations": {"netbird.io/config-revision": revision},
                },
                "spec": pod_spec,
            },
        },
    }


def _secret_env(name: str, secret_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "valueFrom": {
            "secretKeyRef": {"name": secret_ref["name"], "key": secret_ref["key"]}
        },
    }


def _effective_endpoint(item: dict[str, Any], kind: str) -> str:
    if item.get("mode") == "external":
        endpoint = item.get("endpoint")
        if not endpoint:
            raise InstanceError("InvalidSpec", f"external {kind} {item.get('name')!r} requires endpoint")
        return endpoint
    if item.get("publicEndpoint"):
        return item["publicEndpoint"]
    name = item["name"]
    if kind == "signal":
        return f"http://netbird-signal-{name}:33000"
    if kind == "relay":
        return f"rel://netbird-relay-{name}:33080"
    return f"stun:netbird-stun-turn-{name}:3478"


def _host(endpoint: str) -> dict[str, str]:
    parsed = urlparse(endpoint)
    if not parsed.scheme:
        raise InstanceError("InvalidSpec", f"endpoint {endpoint!r} requires a scheme")
    proto = {"grpc": "http", "grpcs": "https"}.get(parsed.scheme, parsed.scheme)
    uri = parsed.netloc or parsed.path
    return {"Proto": proto, "URI": uri}


def _management_config(spec: dict[str, Any]) -> dict[str, Any]:
    signals = spec.get("signals") or []
    if len(signals) > 1:
        raise InstanceError("InvalidSpec", "NetBird 0.78 supports one effective Signal endpoint")
    signal = _host(_effective_endpoint(signals[0], "signal")) if signals else None
    relays = [_effective_endpoint(item, "relay") for item in spec.get("relays") or []]
    stuns: list[dict[str, str]] = []
    turns: list[dict[str, str]] = []
    turn_refs: list[dict[str, str]] = []
    for item in spec.get("stunTurns") or []:
        endpoints = item.get("endpoints") or [_effective_endpoint(item, "stun-turn")]
        for endpoint in endpoints:
            scheme = endpoint.split(":", 1)[0].lower()
            host = {
                "Proto": "tcp" if scheme in {"stuns", "turns"} else "udp",
                "URI": endpoint,
            }
            if scheme in {"turn", "turns"}:
                turns.append(host)
            else:
                stuns.append(host)
        if item.get("credentialSecretRef"):
            turn_refs.append(item["credentialSecretRef"])
    unique_turn_refs = {(ref["name"], ref["key"]) for ref in turn_refs}
    if turns and len(unique_turn_refs) != 1:
        raise InstanceError("InvalidSpec", "TURN endpoints require one shared credentialSecretRef")

    oidc = spec["oidc"]
    issuer = oidc["issuer"].rstrip("/")
    provider_config: dict[str, Any] = {
        "Audience": oidc["clientID"],
        "ClientID": oidc["clientID"],
        "Scope": OIDC_SCOPES,
        "RedirectURLs": oidc.get("redirectURLs") or DEFAULT_PKCE_REDIRECT_URLS,
    }
    if oidc.get("clientSecretRef"):
        provider_config["ClientSecret"] = "{{.OIDC_CLIENT_SECRET}}"

    config: dict[str, Any] = {
        "Stuns": stuns,
        "Relay": {"Addresses": relays, "CredentialsTTL": "24h", "Secret": "{{.NB_RELAY_SECRET}}"},
        "DisableDefaultPolicy": False,
        "DataStoreEncryptionKey": "{{.DATA_STORE_ENCRYPTION_KEY}}",
        "StoreConfig": {"Engine": "postgres"},
        "HttpConfig": {
            "AuthIssuer": issuer,
            "AuthAudience": oidc["clientID"],
            "AuthUserIDClaim": "",
            "OIDCConfigEndpoint": f"{issuer}/.well-known/openid-configuration",
        },
        "PKCEAuthorizationFlow": {"ProviderConfig": provider_config},
    }
    if signal:
        config["Signal"] = signal
    if turns:
        config["TURNConfig"] = {
            "Turns": turns,
            "CredentialsTTL": "12h",
            "Secret": "{{.NB_TURN_SECRET}}",
            "TimeBasedCredentials": True,
        }
    return config


def _turn_secret_ref(spec: dict[str, Any]) -> dict[str, str] | None:
    refs = [
        item["credentialSecretRef"]
        for item in spec.get("stunTurns") or []
        if item.get("credentialSecretRef")
    ]
    return refs[0] if refs else None


def _stable_child_secret(children: dict[str, Any] | None, name: str, key: str) -> tuple[dict[str, Any], str]:
    existing = _existing(children, "Secret", name)
    encoded = ((existing or {}).get("data") or {}).get(key)
    if encoded:
        return existing, base64.b64decode(encoded).decode()
    value = secrets.token_urlsafe(32)
    return _secret_child(name, key, value), value


def _runtime_children(parent: dict[str, Any], children: dict[str, Any] | None, bootstrap_enabled: bool) -> tuple[list[dict[str, Any]], str | None]:
    spec = parent["spec"]
    desired: list[dict[str, Any]] = []
    if spec["management"]["mode"] == "managed" or any(
        relay.get("mode") == "managed" for relay in spec.get("relays") or []
    ):
        relay_secret, _ = _stable_child_secret(children, "netbird-relay-auth", "secret")
        desired.append(relay_secret)
    bootstrap_value: str | None = None
    if bootstrap_enabled and spec["management"]["mode"] == "managed":
        bootstrap_secret, bootstrap_value = _stable_child_secret(
            children, BOOTSTRAP_SECRET_NAME, BOOTSTRAP_SECRET_KEY
        )
        desired.append(bootstrap_secret)

    if spec["management"]["mode"] == "managed":
        management_config = _management_config(spec)
        desired.append(_config_child(management_config))
        env = [
            _secret_env("NB_STORE_ENGINE_POSTGRES_DSN", spec["database"]["secretRef"]),
            _secret_env("DATA_STORE_ENCRYPTION_KEY", spec["encryptionKeySecretRef"]),
            _secret_env("NB_RELAY_SECRET", {"name": "netbird-relay-auth", "key": "secret"}),
        ]
        if spec["oidc"].get("clientSecretRef"):
            env.append(_secret_env("OIDC_CLIENT_SECRET", spec["oidc"]["clientSecretRef"]))
        turn_secret_ref = _turn_secret_ref(spec)
        if turn_secret_ref:
            env.append(_secret_env("NB_TURN_SECRET", turn_secret_ref))
        volumes = [{"name": "config", "configMap": {"name": "netbird-management-config"}}]
        mounts = [{"name": "config", "mountPath": "/etc/netbird", "readOnly": True}]
        if bootstrap_enabled:
            env.append({"name": "NB_RUNIK_BOOTSTRAP_SECRET_FILE", "value": "/var/run/secrets/netbird-bootstrap/token"})
            volumes.append({"name": "bootstrap", "secret": {"secretName": BOOTSTRAP_SECRET_NAME}})
            mounts.append({"name": "bootstrap", "mountPath": "/var/run/secrets/netbird-bootstrap", "readOnly": True})
        desired.extend(
            [
                _deployment(
                    MANAGEMENT_NAME,
                    "management",
                    _image(spec["management"]),
                    args=["--port", str(MANAGEMENT_PORT), "--log-file", "console", "--log-level", "info"],
                    env=env,
                    ports=[MANAGEMENT_PORT],
                    volumes=volumes,
                    mounts=mounts,
                    revision_data=management_config,
                ),
                _service(
                    MANAGEMENT_NAME,
                    "management",
                    MANAGEMENT_PORT,
                    spec["management"].get("service"),
                    port_name="http2-management",
                    app_protocol="http2",
                ),
            ]
        )

    dashboard = spec.get("dashboard")
    if dashboard and dashboard.get("mode") == "managed":
        oidc = spec["oidc"]
        public_management_endpoint = spec["management"].get(
            "publicEndpoint", spec["management"].get("endpoint", "")
        )
        desired.extend(
            [
                _deployment(
                    "netbird-dashboard",
                    "dashboard",
                    _image(dashboard),
                    env=[
                        {"name": "NETBIRD_MGMT_API_ENDPOINT", "value": public_management_endpoint},
                        {"name": "NETBIRD_MGMT_GRPC_API_ENDPOINT", "value": public_management_endpoint},
                        {"name": "AUTH_AUDIENCE", "value": oidc["clientID"]},
                        {"name": "AUTH_CLIENT_ID", "value": oidc["clientID"]},
                        {"name": "AUTH_AUTHORITY", "value": oidc["issuer"]},
                        {"name": "AUTH_SUPPORTED_SCOPES", "value": OIDC_SCOPES},
                        {"name": "AUTH_REDIRECT_URI", "value": DASHBOARD_REDIRECT_URI},
                        {
                            "name": "AUTH_SILENT_REDIRECT_URI",
                            "value": DASHBOARD_SILENT_REDIRECT_URI,
                        },
                        {"name": "USE_AUTH0", "value": "false"},
                    ],
                    ports=[80],
                ),
                _service("netbird-dashboard", "dashboard", 80, dashboard.get("service")),
            ]
        )

    for signal in spec.get("signals") or []:
        if signal["mode"] == "managed":
            name = f"netbird-signal-{signal['name']}"
            desired.extend(
                [
                    _deployment(name, f"signal-{signal['name']}", _image(signal), env=[{"name": "NB_PORT", "value": "33000"}], ports=[33000]),
                    _service(name, f"signal-{signal['name']}", 33000, signal.get("service")),
                ]
            )
    for relay in spec.get("relays") or []:
        if relay["mode"] == "managed":
            name = f"netbird-relay-{relay['name']}"
            endpoint = _effective_endpoint(relay, "relay")
            desired.extend(
                [
                    _deployment(
                        name,
                        f"relay-{relay['name']}",
                        _image(relay),
                        env=[
                            {"name": "NB_LISTEN_ADDRESS", "value": ":33080"},
                            {"name": "NB_EXPOSED_ADDRESS", "value": endpoint.removeprefix("rel://").removeprefix("rels://")},
                            _secret_env("NB_AUTH_SECRET", {"name": "netbird-relay-auth", "key": "secret"}),
                        ],
                        ports=[33080],
                    ),
                    _service(name, f"relay-{relay['name']}", 33080, relay.get("service")),
                ]
            )
    for stun in spec.get("stunTurns") or []:
        if stun["mode"] == "managed":
            name = f"netbird-stun-turn-{stun['name']}"
            desired.extend(
                [
                    _deployment(
                        name,
                        f"stun-turn-{stun['name']}",
                        _image(stun),
                        args=["--listening-port=3478", "--log-file=stdout", "--no-auth", "--fingerprint"],
                        ports=[3478],
                    ),
                    _service(name, f"stun-turn-{stun['name']}", 3478, {**stun.get("service", {}), "protocol": "UDP"}),
                ]
            )
    return desired, bootstrap_value


def _management_url(spec: dict[str, Any]) -> str:
    management = spec["management"]
    if management["mode"] == "external":
        return management["endpoint"].rstrip("/")
    return f"http://{MANAGEMENT_NAME}:{MANAGEMENT_PORT}"


def _management_available(
    children: dict[str, Any] | None,
    managed: bool,
    desired: list[dict[str, Any]],
) -> bool:
    if not managed:
        return True
    deployment = _existing(children, "Deployment", MANAGEMENT_NAME)
    desired_deployment = next(
        child
        for child in desired
        if child.get("kind") == "Deployment"
        and (child.get("metadata") or {}).get("name") == MANAGEMENT_NAME
    )
    observed_revision = (
        (((deployment or {}).get("spec") or {}).get("template") or {})
        .get("metadata", {})
        .get("annotations", {})
        .get("netbird.io/config-revision")
    )
    desired_revision = (
        desired_deployment["spec"]["template"]["metadata"]["annotations"]
        ["netbird.io/config-revision"]
    )
    return bool(
        observed_revision == desired_revision
        and (deployment or {}).get("status", {}).get("availableReplicas", 0) >= 1
    )


def _management_stage(
    desired: list[dict[str, Any]], children: dict[str, Any] | None
) -> list[dict[str, Any]]:
    dependency_names = {
        BOOTSTRAP_SECRET_NAME,
        "netbird-relay-auth",
        "netbird-management-config",
        MANAGEMENT_NAME,
    }
    staged = [
        child
        for child in desired
        if (child.get("metadata") or {}).get("name") in dependency_names
    ]
    staged_keys = {
        (child.get("apiVersion"), child.get("kind"), (child.get("metadata") or {}).get("name"))
        for child in staged
    }
    for child in _flatten_children(children):
        key = (child.get("apiVersion"), child.get("kind"), (child.get("metadata") or {}).get("name"))
        if key not in staged_keys:
            staged.append(child)
    return staged


class BootstrapClient:
    def __init__(self, base_url: str, timeout: float = 10.0, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()

    def _request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        bootstrap_secret: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if bootstrap_secret:
            headers["X-Runik-Bootstrap-Secret"] = bootstrap_secret
        try:
            response = self.session.request(
                method,
                f"{self.base_url}/api/runik/bootstrap{path}",
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise InstanceError("ManagementUnavailable", "bootstrap API is unavailable") from exc
        if response.status_code >= 400:
            raise InstanceError("BootstrapAPIError", f"bootstrap API returned HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise InstanceError("BootstrapAPIError", "bootstrap API returned an invalid response")
        return data

    def status(self, instance_id: str) -> dict[str, Any]:
        try:
            response = self.session.get(
                f"{self.base_url}/api/runik/bootstrap",
                params={"instance_id": instance_id},
                headers={"Accept": "application/json"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise InstanceError("ManagementUnavailable", "bootstrap status is unavailable") from exc
        if response.status_code >= 400:
            raise InstanceError("ManagementUnavailable", f"bootstrap status returned HTTP {response.status_code}")
        return response.json()

    def bootstrap(self, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "", token=token, payload=payload)

    def adopt(self, pat: str, bootstrap_secret: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "POST",
            "/adopt",
            token=pat,
            bootstrap_secret=bootstrap_secret,
            payload=payload,
        )

    def ack(self, instance_id: str, pat: str) -> dict[str, Any]:
        return self._request("POST", "/ack", token=pat, payload={"instance_id": instance_id})

    def verify_credential(self, instance_id: str, pat: str) -> dict[str, Any]:
        return self._request(
            "POST", "/verify", token=pat, payload={"instance_id": instance_id}
        )

    def prepare_credential_rotation(
        self, instance_id: str, revision: str, pat: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/rotate",
            token=pat,
            payload={"instance_id": instance_id, "revision": revision},
        )

    def complete_credential_rotation(
        self, instance_id: str, revision: str, pat: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/rotate/ack",
            token=pat,
            payload={"instance_id": instance_id, "revision": revision},
        )


class KubernetesSecretStore:
    def __init__(self, session: requests.Session | None = None):
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if not host:
            raise InstanceError("KubernetesUnavailable", "KUBERNETES_SERVICE_HOST is not set")
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        try:
            with open(token_path, encoding="utf-8") as stream:
                token = stream.read().strip()
        except OSError as exc:
            raise InstanceError("KubernetesUnavailable", "service account token is unavailable") from exc
        self.api_host = f"https://{host}:{port}"
        self.base_url = f"{self.api_host}/api/v1"
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.verify = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        self.session = session or requests.Session()

    def get(self, namespace: str, name: str) -> dict[str, Any] | None:
        response = self.session.get(
            f"{self.base_url}/namespaces/{namespace}/secrets/{name}",
            headers=self.headers,
            verify=self.verify,
            timeout=10,
        )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise InstanceError("KubernetesUnavailable", f"Secret read returned HTTP {response.status_code}")
        return response.json()

    def create(self, namespace: str, name: str, key: str, value: str) -> dict[str, Any]:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": namespace},
            "type": "Opaque",
            "data": {key: base64.b64encode(value.encode()).decode()},
        }
        headers = {**self.headers, "Content-Type": "application/json"}
        response = self.session.post(
            f"{self.base_url}/namespaces/{namespace}/secrets",
            headers=headers,
            json=body,
            verify=self.verify,
            timeout=10,
        )
        if response.status_code == 409:
            existing = self.get(namespace, name)
            if existing is not None:
                return existing
        if response.status_code >= 400:
            raise InstanceError("KubernetesUnavailable", f"Secret create returned HTTP {response.status_code}")
        return response.json()

    def update(
        self,
        namespace: str,
        name: str,
        key: str,
        value: str,
        annotations: dict[str, str],
    ) -> dict[str, Any]:
        body = {
            "metadata": {"annotations": annotations},
            "data": {key: base64.b64encode(value.encode()).decode()},
        }
        headers = {**self.headers, "Content-Type": "application/merge-patch+json"}
        response = self.session.patch(
            f"{self.base_url}/namespaces/{namespace}/secrets/{name}",
            headers=headers,
            json=body,
            verify=self.verify,
            timeout=10,
        )
        if response.status_code >= 400:
            raise InstanceError(
                "KubernetesUnavailable",
                f"Secret update returned HTTP {response.status_code}",
            )
        return response.json()

    def orphan(self, namespace: str, parent_uid: str, children: list[dict[str, Any]]) -> None:
        resource_paths = {
            ("apps/v1", "Deployment"): "apis/apps/v1/namespaces/{namespace}/deployments/{name}",
            ("v1", "Service"): "api/v1/namespaces/{namespace}/services/{name}",
            ("v1", "ConfigMap"): "api/v1/namespaces/{namespace}/configmaps/{name}",
            ("v1", "Secret"): "api/v1/namespaces/{namespace}/secrets/{name}",
        }
        headers = {**self.headers, "Content-Type": "application/merge-patch+json"}
        for child in children:
            metadata = child.get("metadata") or {}
            owner_refs = metadata.get("ownerReferences") or []
            retained_refs = [ref for ref in owner_refs if ref.get("uid") != parent_uid]
            if len(retained_refs) == len(owner_refs):
                continue
            resource_path = resource_paths.get((child.get("apiVersion"), child.get("kind")))
            name = metadata.get("name")
            if not resource_path or not name:
                raise InstanceError("KubernetesUnavailable", "cannot orphan an unknown runtime child")
            url = f"{self.api_host}/{resource_path.format(namespace=quote(namespace, safe=''), name=quote(name, safe=''))}"
            response = self.session.patch(
                url,
                headers=headers,
                json={"metadata": {"ownerReferences": retained_refs}},
                verify=self.verify,
                timeout=10,
            )
            if response.status_code == 404:
                continue
            if response.status_code >= 400:
                raise InstanceError(
                    "KubernetesUnavailable",
                    f"{child.get('kind')} {name} orphan returned HTTP {response.status_code}",
                )


def _secret_plain(secret: dict[str, Any] | None, key: str) -> str | None:
    encoded = ((secret or {}).get("data") or {}).get(key)
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded, validate=True).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise InstanceError("RecoveryRequired", "credential Secret contains invalid data") from exc


def _identity(parent: dict[str, Any]) -> tuple[str, str]:
    metadata = parent.get("metadata") or {}
    instance_id = f"{metadata.get('namespace', 'default')}/{metadata.get('name', '')}"
    account_id = "runik-" + hashlib.sha256(instance_id.encode()).hexdigest()[:24]
    return instance_id, account_id


def _previous_recovery_required(parent: dict[str, Any]) -> bool:
    status = parent.get("status") or {}
    return status.get("phase") == "Degraded" and any(
        condition.get("reason") == "RecoveryRequired"
        for condition in status.get("conditions") or []
    )


def _reconcile_sealed_credential(
    parent: dict[str, Any],
    client: BootstrapClient,
    store: KubernetesSecretStore,
    instance_id: str,
    namespace: str,
    credential_ref: dict[str, Any],
) -> tuple[dict[str, Any], str, str, bool, bool]:
    bootstrap = (parent.get("spec") or {}).get("bootstrap") or {}
    rotation_revision = bootstrap.get("rotationRevision", "")
    recovery_revision = bootstrap.get("recoveryRevision", "")
    if not isinstance(rotation_revision, str) or not isinstance(recovery_revision, str):
        raise InstanceError(
            "InvalidSpec", "credential rotation and recovery revisions must be strings"
        )

    previous = parent.get("status") or {}
    observed_rotation = (
        (previous.get("credentialRotation") or {}).get("observedRevision") or ""
    )
    observed_recovery = (
        (previous.get("credentialRecovery") or {}).get("observedRevision") or ""
    )
    owner_secret = store.get(namespace, credential_ref["name"])
    owner_pat = _secret_plain(owner_secret, credential_ref["key"])
    if not owner_pat:
        raise InstanceError(
            "RecoveryRequired", "sealed operator Owner credential Secret is unavailable"
        )

    rotated = rotation_revision != observed_rotation
    annotations = dict(
        ((owner_secret or {}).get("metadata") or {}).get("annotations") or {}
    )
    resume_rotation = (
        rotated
        and annotations.get(ROTATION_REVISION_ANNOTATION) == rotation_revision
    )
    if resume_rotation:
        try:
            remote = client.complete_credential_rotation(
                instance_id, rotation_revision, owner_pat
            )
        except InstanceError as exc:
            raise InstanceError(
                "CredentialRotationFailed",
                "replacement operator Owner credential could not be confirmed",
            ) from exc
    else:
        try:
            remote = client.verify_credential(instance_id, owner_pat)
        except InstanceError as exc:
            raise InstanceError(
                "RecoveryRequired", "sealed operator Owner credential is not active"
            ) from exc

        if rotated:
            try:
                prepared = client.prepare_credential_rotation(
                    instance_id, rotation_revision, owner_pat
                )
            except InstanceError as exc:
                raise InstanceError(
                    "CredentialRotationFailed",
                    "replacement operator Owner credential could not be prepared",
                ) from exc
            replacement = prepared.get("personal_access_token")
            if not replacement:
                raise InstanceError(
                    "CredentialRotationFailed",
                    "Management omitted the replacement operator Owner credential",
                )
            annotations[ROTATION_REVISION_ANNOTATION] = rotation_revision
            stored = store.update(
                namespace,
                credential_ref["name"],
                credential_ref["key"],
                replacement,
                annotations,
            )
            if _secret_plain(stored, credential_ref["key"]) != replacement:
                raise InstanceError(
                    "CredentialRotationFailed",
                    "Owner Secret write did not preserve the replacement PAT",
                )
            if (
                ((stored.get("metadata") or {}).get("annotations") or {}).get(
                    ROTATION_REVISION_ANNOTATION
                )
                != rotation_revision
            ):
                raise InstanceError(
                    "CredentialRotationFailed",
                    "Owner Secret write did not preserve the rotation revision",
                )
            owner_pat = replacement
            try:
                remote = client.complete_credential_rotation(
                    instance_id, rotation_revision, owner_pat
                )
            except InstanceError as exc:
                raise InstanceError(
                    "CredentialRotationFailed",
                    "replacement operator Owner credential could not be confirmed",
                ) from exc

    recovered = _previous_recovery_required(parent)
    if recovered and recovery_revision == observed_recovery:
        raise InstanceError(
            "RecoveryRequired",
            "restore the Owner Secret and change bootstrap.recoveryRevision to acknowledge recovery",
        )
    if rotated:
        if remote.get("phase") != "sealed":
            raise InstanceError(
                "CredentialRotationFailed",
                "Management did not confirm credential rotation",
            )

    return remote, rotation_revision, recovery_revision, rotated, recovered


def sync_with_children(
    parent: dict[str, Any],
    children: dict[str, Any] | None,
    *,
    bootstrap_client: BootstrapClient | None = None,
    secret_store: KubernetesSecretStore | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    metadata = parent.get("metadata") or {}
    if metadata.get("name") != "default":
        raise InstanceError("InvalidSpec", "NetBirdInstance metadata.name must be 'default'")
    operator_namespace = os.environ.get("POD_NAMESPACE")
    if operator_namespace and metadata.get("namespace", "default") != operator_namespace:
        raise InstanceError(
            "InvalidSpec",
            f"NetBirdInstance must be in operator namespace {operator_namespace!r}",
        )
    spec = parent.get("spec") or {}
    management = spec.get("management") or {}
    if management.get("mode") not in {"managed", "external"}:
        raise InstanceError("InvalidSpec", "management.mode must be managed or external")
    credential_ref = (spec.get("bootstrap") or {}).get("credentialSecretRef") or {}
    adoption_ref = (spec.get("bootstrap") or {}).get("adoptionCredentialSecretRef")
    if adoption_ref and adoption_ref == credential_ref:
        raise InstanceError(
            "InvalidSpec", "adoption and output credential Secrets must be different"
        )
    configured_credential = (
        os.environ.get("NB_API_KEY_SECRET_NAME"),
        os.environ.get("NB_API_KEY_SECRET_KEY"),
    )
    if all(configured_credential) and configured_credential != (
        credential_ref.get("name"),
        credential_ref.get("key"),
    ):
        raise InstanceError(
            "InvalidSpec",
            "bootstrap.credentialSecretRef must match the operator API credential Secret",
        )

    old_bootstrap = ((parent.get("status") or {}).get("bootstrap") or {}).get("phase")
    bootstrap_enabled = old_bootstrap != "BootstrapSealed"
    desired, bootstrap_value = _runtime_children(parent, children, bootstrap_enabled)
    if not _management_available(children, management["mode"] == "managed", desired):
        return (
            _status(parent, phase="Progressing", bootstrap_phase="BootstrapPending", ready=False, reason="WaitingForManagement", message="Management Deployment is not available"),
            _management_stage(desired, children),
            10,
        )

    instance_id, account_id = _identity(parent)
    client = bootstrap_client or BootstrapClient(_management_url(spec))
    try:
        remote = client.status(instance_id)
    except (requests.RequestException, ValueError) as exc:
        raise InstanceError("ManagementUnavailable", "Management bootstrap status is unavailable") from exc

    remote_phase = remote.get("phase", "pending")
    if remote_phase == "sealed":
        desired, _ = _runtime_children(parent, children, False)
        had_bootstrap_mount = bool(
            _existing(children, "Secret", BOOTSTRAP_SECRET_NAME)
            or old_bootstrap != "BootstrapSealed"
        )
        ready = not had_bootstrap_mount
        if ready:
            namespace = metadata.get("namespace", "default")
            store = secret_store or KubernetesSecretStore()
            (
                remote,
                rotation_revision,
                recovery_revision,
                rotated,
                recovered,
            ) = _reconcile_sealed_credential(
                parent, client, store, instance_id, namespace, credential_ref
            )
        else:
            rotation_revision = ""
            recovery_revision = ""
            rotated = False
            recovered = False
        status = _status(
            parent,
            phase="Ready" if ready else "Progressing",
            bootstrap_phase="BootstrapSealed",
            ready=ready,
            runtime_ready=True,
            reason="Reconciled" if ready else "RemovingBootstrapChannel",
            identity=remote,
        )
        if ready:
            status = _credential_status(
                parent,
                status,
                rotation_revision=rotation_revision,
                recovery_revision=recovery_revision,
                rotated=rotated,
                recovered=recovered,
            )
        return (
            status,
            desired,
            10 if not ready else 300,
        )
    if remote_phase not in {"pending", "bootstrapping"}:
        raise InstanceError("RecoveryRequired", f"unknown persisted bootstrap phase {remote_phase!r}")

    namespace = metadata.get("namespace", "default")
    store = secret_store or KubernetesSecretStore()
    owner_secret = store.get(namespace, credential_ref["name"])
    owner_pat = _secret_plain(owner_secret, credential_ref["key"])

    if owner_pat:
        try:
            remote = client.ack(instance_id, owner_pat)
        except InstanceError as exc:
            raise InstanceError("RecoveryRequired", "Owner Secret does not match the active bootstrap PAT") from exc
    else:
        if owner_secret is not None:
            raise InstanceError("RecoveryRequired", "Owner Secret exists without the declared key")
        if management["mode"] == "external":
            bootstrap_ref = management.get("bootstrapSecretRef") or {}
            bootstrap_value = _secret_plain(store.get(namespace, bootstrap_ref.get("name", "")), bootstrap_ref.get("key", "token"))
        if not bootstrap_value:
            raise InstanceError("BootstrapPending", "bootstrap credential is not available")
        bootstrap_spec = spec["bootstrap"]
        adoption_ref = bootstrap_spec.get("adoptionCredentialSecretRef")
        if adoption_ref:
            adoption_pat = _secret_plain(
                store.get(namespace, adoption_ref["name"]), adoption_ref["key"]
            )
            if not adoption_pat:
                raise InstanceError(
                    "BootstrapPending", "existing Owner adoption credential is not available"
                )
            remote = client.adopt(
                adoption_pat,
                bootstrap_value,
                {"instance_id": instance_id, "account_name": bootstrap_spec["accountName"]},
            )
        else:
            remote = client.bootstrap(
                bootstrap_value,
                {
                    "instance_id": instance_id,
                    "account_id": account_id,
                    "account_name": bootstrap_spec["accountName"],
                },
            )
        owner_pat = remote.get("personal_access_token")
        if not owner_pat:
            raise InstanceError("RecoveryRequired", "bootstrap response omitted the one-time PAT")
        stored = store.create(namespace, credential_ref["name"], credential_ref["key"], owner_pat)
        if _secret_plain(stored, credential_ref["key"]) != owner_pat:
            raise InstanceError("RecoveryRequired", "Owner Secret write did not preserve the bootstrap PAT")
        remote = client.ack(instance_id, owner_pat)

    if remote.get("phase") != "sealed":
        raise InstanceError("RecoveryRequired", "Management did not persist the bootstrap seal")
    desired, _ = _runtime_children(parent, children, False)
    return (
        _status(
            parent,
            phase="Progressing",
            bootstrap_phase="BootstrapSealed",
            ready=False,
            runtime_ready=True,
            reason="RemovingBootstrapChannel",
            identity=remote,
        ),
        desired,
        10,
    )


def _flatten_children(children: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [item for objects in (children or {}).values() for item in objects.values()]


def _has_bootstrap_channel(children: dict[str, Any] | None) -> bool:
    if _existing(children, "Secret", BOOTSTRAP_SECRET_NAME):
        return True
    management = _existing(children, "Deployment", MANAGEMENT_NAME) or {}
    pod_spec = (((management.get("spec") or {}).get("template") or {}).get("spec") or {})
    if any(volume.get("name") == "bootstrap" for volume in pod_spec.get("volumes") or []):
        return True
    for container in pod_spec.get("containers") or []:
        if any(env.get("name") == "NB_RUNIK_BOOTSTRAP_SECRET_FILE" for env in container.get("env") or []):
            return True
    return False


def finalize(
    parent: dict[str, Any],
    children: dict[str, Any] | None,
    *,
    ownership_store: KubernetesSecretStore | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    policy = ((parent.get("spec") or {}).get("lifecycle") or {}).get("deletionPolicy", "Retain")
    observed = _flatten_children(children)
    if policy == "Retain":
        # Never retain a mounted bootstrap credential. Let Metacontroller remove
        # that Secret and roll Management before ownership is released.
        if _has_bootstrap_channel(children):
            desired, _ = _runtime_children(parent, children, False)
            return {"phase": "Deleting", "retainedRuntime": False}, desired, False
        if observed:
            metadata = parent.get("metadata") or {}
            parent_uid = metadata.get("uid")
            if not parent_uid:
                raise InstanceError("KubernetesUnavailable", "NetBirdInstance UID is unavailable")
            store = ownership_store or KubernetesSecretStore()
            store.orphan(metadata.get("namespace", "default"), parent_uid, observed)
        return {"phase": "Deleting", "retainedRuntime": True}, [], True

    # Returning no desired children with finalized=false gives Metacontroller a
    # reconciliation pass in which it deletes the managed runtime. Only release
    # the parent finalizer after its child cache is empty.
    if observed:
        return {"phase": "Deleting", "retainedRuntime": False}, [], False
    return {"phase": "Deleting", "retainedRuntime": False}, [], True
