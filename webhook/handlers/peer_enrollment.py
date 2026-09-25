"""Declarative local enrollment and remote peer adoption.

Enrollment credentials are deliberately short lived: a one-off NetBird setup
key is exposed only through a same-namespace Secret child while the expected
workload is waiting to enroll.  Once a unique peer is observed, the key is
revoked and the Secret is omitted from the desired children.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from typing import Any

from ..netbird_client import NotFound, Resolver
from .peer_group_binding import _set_membership


def _condition(kind: str, status: str, reason: str, message: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"type": kind, "status": status, "reason": reason}
    if message:
        result["message"] = message[:2000]
    return result


def _phase_status(
    parent: dict[str, Any], phase: str, reason: str, message: str = "", **values: Any
) -> dict[str, Any]:
    ready = phase == "Ready"
    status = dict(parent.get("status") or {})
    status.update(values)
    status.update(
        phase=phase,
        synced=ready,
        observedGeneration=(parent.get("metadata") or {}).get("generation"),
        conditions=[
            _condition("Ready", "True" if ready else "False", reason, message),
            _condition("Enrolled", "True" if ready else "False", reason, message),
            _condition(
                "Degraded",
                "True" if phase in {"Conflict", "Degraded"} else "False",
                reason,
                message,
            ),
        ],
    )
    return status


def _secret(name: str, key: str, value: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": name,
            "labels": {"app.kubernetes.io/managed-by": "netbird-complementary-operator"},
        },
        "type": "Opaque",
        "data": {key: base64.b64encode(value.encode()).decode()},
    }


def _existing_secret(children: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    for object_map in (children or {}).values():
        if not isinstance(object_map, Mapping):
            continue
        for child in object_map.values():
            metadata = child.get("metadata") if isinstance(child, Mapping) else None
            if (
                isinstance(child, Mapping)
                and child.get("kind") == "Secret"
                and metadata
                and metadata.get("name") == name
            ):
                return dict(child)
    return None


def _find_related_workload(
    related: dict[str, Any] | None, api_version: str, kind: str, namespace: str, name: str
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for object_map in (related or {}).values():
        if not isinstance(object_map, Mapping):
            continue
        for obj in object_map.values():
            metadata = obj.get("metadata") if isinstance(obj, Mapping) else None
            if (
                isinstance(metadata, Mapping)
                and obj.get("apiVersion") == api_version
                and obj.get("kind") == kind
                and metadata.get("namespace") == namespace
                and metadata.get("name") == name
            ):
                matches.append(dict(obj))
    return matches[0] if len(matches) == 1 else None


def _references_secret(value: Any, secret_name: str) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in {"secretKeyRef", "secretRef"} and isinstance(child, Mapping):
                if child.get("name") == secret_name:
                    return True
            if key == "secret" and isinstance(child, Mapping):
                if child.get("secretName") == secret_name:
                    return True
            if _references_secret(child, secret_name):
                return True
    elif isinstance(value, list):
        return any(_references_secret(item, secret_name) for item in value)
    return False


def _setup_key_name(parent: dict[str, Any]) -> str:
    metadata = parent.get("metadata") or {}
    uid = re.sub(
        r"[^a-z0-9-]", "", str(metadata.get("uid") or metadata.get("name") or "peer").lower()
    )
    return f"runik-enroll-{uid[:32]}"


def _peers(resolver: Resolver) -> list[dict[str, Any]]:
    result = resolver.client.get("/peers") or []
    if not isinstance(result, list) or any(not isinstance(item, Mapping) for item in result):
        raise ValueError("NetBird returned an invalid peer list")
    return [dict(item) for item in result]


def _matches_identity(peer: Mapping[str, Any], identity: Mapping[str, Any]) -> bool:
    hostname = identity.get("hostname")
    if hostname and peer.get("hostname") != hostname and peer.get("name") != hostname:
        return False
    for spec_key, api_key in (("dnsLabel", "dns_label"), ("serialNumber", "serial_number")):
        expected = identity.get(spec_key)
        if expected and peer.get(api_key) != expected:
            return False
    return True


def _verify_identity(peer: Mapping[str, Any], identity: Mapping[str, Any]) -> str | None:
    if identity.get("id") and peer.get("id") != identity["id"]:
        return f"peer ID {peer.get('id')!r} does not match {identity['id']!r}"
    if not _matches_identity(peer, identity):
        return "the peer does not match the declared hostname, DNS label, or serial number"
    return None


def _profile(peer: Mapping[str, Any]) -> dict[str, Any]:
    if not peer.get("id") or not peer.get("ip") or not peer.get("dns_label"):
        raise ValueError(
            "peer WireGuard profile is incomplete (id, ip, and dns_label are required)"
        )
    return {
        key: peer[api_key]
        for key, api_key in (
            ("ip", "ip"),
            ("ipv6", "ipv6"),
            ("dnsLabel", "dns_label"),
            ("version", "version"),
            ("connected", "connected"),
        )
        if api_key in peer
    }


def _desired_peer(current: Mapping[str, Any], settings: Mapping[str, Any]) -> dict[str, Any]:
    desired = {
        "name": current.get("name") or current.get("hostname") or current["id"],
        "ssh_enabled": bool(current.get("ssh_enabled", False)),
        "login_expiration_enabled": bool(current.get("login_expiration_enabled", False)),
        "inactivity_expiration_enabled": bool(current.get("inactivity_expiration_enabled", False)),
    }
    for spec_key, api_key in (
        ("name", "name"),
        ("sshEnabled", "ssh_enabled"),
        ("loginExpirationEnabled", "login_expiration_enabled"),
        ("inactivityExpirationEnabled", "inactivity_expiration_enabled"),
        ("approvalRequired", "approval_required"),
    ):
        if spec_key in settings:
            desired[api_key] = settings[spec_key]
    return desired


def _reconcile_settings(
    parent: dict[str, Any], resolver: Resolver, peer: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    spec = parent.get("spec") or {}
    old_status = parent.get("status") or {}
    settings = spec.get("settings") or {}
    original = old_status.get("originalSettings")
    if original is None:
        original = {
            key: peer.get(key)
            for key in (
                "name",
                "ssh_enabled",
                "login_expiration_enabled",
                "inactivity_expiration_enabled",
                "approval_required",
            )
            if key in peer
        }
    desired = _desired_peer(peer, settings)
    if any(peer.get(key) != value for key, value in desired.items()):
        updated = resolver.client.put(f"/peers/{peer['id']}", desired)
        if isinstance(updated, Mapping):
            peer = {**peer, **updated}

    desired_group_ids = resolver.group_ids(spec.get("groupRefs") or [])
    old_managed = set(old_status.get("managedGroupIds") or [])
    managed = set(old_managed)
    for group_id in desired_group_ids:
        if _set_membership(resolver, group_id, str(peer["id"]), True):
            managed.add(group_id)
    for group_id in sorted(old_managed - set(desired_group_ids)):
        try:
            _set_membership(resolver, group_id, str(peer["id"]), False)
        except NotFound:
            pass
        managed.discard(group_id)
    return peer, {"originalSettings": original, "managedGroupIds": sorted(managed)}


def _revoke_setup_key(resolver: Resolver, key_id: str, group_ids: list[str]) -> None:
    try:
        resolver.client.put(
            f"/setup-keys/{key_id}", {"auto_groups": group_ids, "revoked": True}
        )
    except NotFound:
        pass


def _ready(
    parent: dict[str, Any], resolver: Resolver, peer: dict[str, Any], *, owned: bool
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    identity_error = _verify_identity(peer, (parent.get("spec") or {}).get("peerIdentity") or {})
    if identity_error:
        return _phase_status(parent, "Conflict", "IdentityConflict", identity_error), []
    profile = _profile(peer)
    if owned:
        # A newly enrolled peer had no pre-existing membership: seed ownership
        # with every auto-group on its one-off key. This also lets the normal
        # diff remove a group that was dropped while enrollment was in flight.
        settings_parent = dict(parent)
        settings_status = dict(parent.get("status") or {})
        settings_status["managedGroupIds"] = sorted(
            set(settings_status.get("managedGroupIds") or [])
            | set(settings_status.get("setupKeyGroupIds") or [])
        )
        settings_parent["status"] = settings_status
    else:
        settings_parent = parent
    peer, ownership = _reconcile_settings(settings_parent, resolver, peer)
    values = {
        "peerId": str(peer["id"]),
        "peerOwned": owned,
        "wireGuardProfile": profile,
        **ownership,
    }
    return _phase_status(parent, "Ready", "PeerReady", **values), []


def _adopt(
    parent: dict[str, Any], resolver: Resolver
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = parent.get("spec") or {}
    identity = spec.get("peerIdentity") or {}
    peer_id = str(identity["id"])
    peer = resolver.client.get(f"/peers/{peer_id}")
    if not isinstance(peer, Mapping):
        raise ValueError("NetBird returned an invalid peer")
    hostname = identity.get("hostname")
    if hostname:
        duplicates = [
            item
            for item in _peers(resolver)
            if item.get("hostname") == hostname or item.get("name") == hostname
        ]
        if len(duplicates) > 1:
            ids = ", ".join(sorted(str(item.get("id")) for item in duplicates))
            return _phase_status(
                parent,
                "Conflict",
                "DuplicateHostname",
                f"hostname {hostname!r} matches multiple peers: {ids}",
            ), []
    return _ready(
        parent,
        resolver,
        dict(peer),
        owned=bool((spec.get("ownership") or {}).get("takeOwnership", False)),
    )


def _create_setup_key(
    parent: dict[str, Any], resolver: Resolver, baseline: list[str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = parent.get("spec") or {}
    enrollment = spec.get("enrollment") or {}
    secret_ref = enrollment.get("secretRef") or {}
    group_ids = resolver.group_ids(spec.get("groupRefs") or [])
    setup_key_name = _setup_key_name(parent)

    # Revoke an orphan from a response that was lost before status persisted.
    for key in resolver.client.get("/setup-keys") or []:
        if key.get("name") == setup_key_name and key.get("id") and not key.get("revoked"):
            _revoke_setup_key(resolver, str(key["id"]), list(key.get("auto_groups") or []))

    created = resolver.client.post(
        "/setup-keys",
        {
            "name": setup_key_name,
            "type": "one-off",
            "expires_in": int(enrollment.get("expiresInSeconds", 600)),
            "usage_limit": 1,
            "auto_groups": group_ids,
            "ephemeral": bool(enrollment.get("ephemeral", False)),
            "allow_extra_dns_labels": False,
        },
    )
    if not isinstance(created, Mapping) or not created.get("id") or not created.get("key"):
        raise ValueError("NetBird returned an incomplete setup key")
    child = _secret(
        str(secret_ref["name"]),
        str(secret_ref.get("key", "setupKey")),
        str(created["key"]),
    )
    return _phase_status(
        parent,
        "Enrolling",
        "SetupKeyIssued",
        setupKeyId=str(created["id"]),
        setupKeyName=setup_key_name,
        setupKeyRevoked=False,
        setupKeyGroupIds=group_ids,
        baselinePeerIds=baseline,
        observedReplacementRevision=int(enrollment.get("replacementRevision", 0)),
    ), [child]


def _enroll(
    parent: dict[str, Any],
    resolver: Resolver,
    children: dict[str, Any] | None,
    related: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    identity = spec.get("peerIdentity") or {}
    enrollment = spec.get("enrollment") or {}
    secret_ref = enrollment.get("secretRef") or {}
    workload_ref = spec.get("workloadRef") or {}
    metadata = parent.get("metadata") or {}

    peer_id = status.get("peerId")
    if peer_id:
        try:
            peer = resolver.client.get(f"/peers/{peer_id}")
        except NotFound:
            replacement = int(enrollment.get("replacementRevision", 0))
            observed = int(status.get("observedReplacementRevision", 0))
            if replacement > observed:
                retry_parent = dict(parent)
                retry_parent["status"] = {
                    "observedReplacementRevision": replacement,
                }
                return _enroll(retry_parent, resolver, children, related)
            return _phase_status(
                parent,
                "Degraded",
                "PeerMissing",
                "the enrolled peer is missing; change "
                "enrollment.replacementRevision to authorize replacement",
            ), []
        return _ready(parent, resolver, dict(peer), owned=True)

    workload = _find_related_workload(
        related,
        str(workload_ref.get("apiVersion")),
        str(workload_ref.get("kind")),
        str(metadata.get("namespace")),
        str(workload_ref.get("name")),
    )
    if workload is None:
        if status.get("setupKeyId") and not status.get("setupKeyRevoked"):
            _revoke_setup_key(
                resolver,
                str(status["setupKeyId"]),
                list(status.get("setupKeyGroupIds") or []),
            )
            return _phase_status(
                parent,
                "Degraded",
                "WorkloadLost",
                "the enrollment workload disappeared; the setup key was revoked",
                setupKeyRevoked=True,
            ), []
        return (
            _phase_status(
                parent,
                "Pending",
                "WorkloadNotFound",
                "the same-namespace enrollment workload was not observed",
            ),
            [],
        )
    if not _references_secret(workload, str(secret_ref.get("name"))):
        if status.get("setupKeyId") and not status.get("setupKeyRevoked"):
            _revoke_setup_key(
                resolver,
                str(status["setupKeyId"]),
                list(status.get("setupKeyGroupIds") or []),
            )
        return _phase_status(
            parent,
            "Conflict",
            "SecretNotConsumed",
            f"workload does not reference enrollment Secret {secret_ref.get('name')!r}",
            setupKeyRevoked=bool(status.get("setupKeyId")),
        ), []

    peers = _peers(resolver)
    baseline = set(status.get("baselinePeerIds") or [])
    if not status.get("setupKeyId"):
        existing = [peer for peer in peers if _matches_identity(peer, identity)]
        if existing:
            ids = ", ".join(sorted(str(peer.get("id")) for peer in existing))
            return _phase_status(
                parent,
                "Conflict",
                "PeerAlreadyExists",
                f"declared enrollment identity already matches peer(s) {ids}; use mode adopt",
            ), []
        return _create_setup_key(
            parent, resolver, sorted(str(peer["id"]) for peer in peers if peer.get("id"))
        )

    candidates = [
        peer
        for peer in peers
        if peer.get("id") not in baseline and _matches_identity(peer, identity)
    ]
    if len(candidates) > 1:
        group_ids = list(status.get("setupKeyGroupIds") or [])
        _revoke_setup_key(resolver, str(status["setupKeyId"]), group_ids)
        return _phase_status(
            parent,
            "Conflict",
            "DuplicateHostname",
            "multiple newly enrolled peers match the declared identity",
            setupKeyRevoked=True,
        ), []
    if len(candidates) == 1:
        group_ids = list(status.get("setupKeyGroupIds") or [])
        _revoke_setup_key(resolver, str(status["setupKeyId"]), group_ids)
        ready, _ = _ready(parent, resolver, candidates[0], owned=True)
        ready["setupKeyRevoked"] = True
        ready.pop("baselinePeerIds", None)
        return ready, []

    key = resolver.client.get(f"/setup-keys/{status['setupKeyId']}")
    if key.get("revoked") or key.get("state") in {"expired", "overused", "revoked"}:
        return _phase_status(
            parent,
            "Conflict",
            "EnrollmentCredentialConsumed",
            "the setup key can no longer enroll the declared peer",
            setupKeyRevoked=True,
        ), []
    existing = _existing_secret(children, str(secret_ref["name"]))
    if existing is None:
        _revoke_setup_key(
            resolver,
            str(status["setupKeyId"]),
            list(status.get("setupKeyGroupIds") or []),
        )
        return _phase_status(
            parent,
            "Degraded",
            "EnrollmentSecretLost",
            "the one-time enrollment Secret disappeared and cannot be reconstructed",
            setupKeyRevoked=True,
        ), []
    return _phase_status(parent, "Enrolling", "WaitingForPeer"), [existing]


def sync_with_children(
    parent: dict[str, Any],
    resolver: Resolver,
    children: dict[str, Any] | None,
    related: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    mode = (parent.get("spec") or {}).get("mode")
    if mode == "adopt":
        return _adopt(parent, resolver)
    if mode == "enroll":
        return _enroll(parent, resolver, children, related)
    raise ValueError("mode must be enroll or adopt")


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    setup_key_id = status.get("setupKeyId")
    if setup_key_id and not status.get("setupKeyRevoked"):
        group_ids = list(status.get("setupKeyGroupIds") or [])
        _revoke_setup_key(resolver, str(setup_key_id), group_ids)

    peer_id = status.get("peerId")
    if not peer_id:
        return {}
    if spec.get("deletionPolicy", "Retain") == "Delete":
        try:
            resolver.client.delete(f"/peers/{peer_id}")
        except NotFound:
            pass
        return {}

    for group_id in status.get("managedGroupIds") or []:
        try:
            _set_membership(resolver, str(group_id), str(peer_id), False)
        except NotFound:
            pass
    if spec.get("restoreSettingsOnDelete", True) and status.get("originalSettings"):
        try:
            current = resolver.client.get(f"/peers/{peer_id}")
            desired = _desired_peer(current, {})
            desired.update(status["originalSettings"])
            resolver.client.put(f"/peers/{peer_id}", desired)
        except NotFound:
            pass
    return {}
