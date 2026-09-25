"""Handler for NetBirdPeer.

NetBird peers self-register via setup keys — this CRD CANNOT create a peer,
it only updates the settings of an existing peer matched by name.
"""

from __future__ import annotations

from typing import Any

from ..netbird_client import NetBirdError, NotFound, Resolver


REQUEST_KEYS = {
    "name",
    "ssh_enabled",
    "login_expiration_enabled",
    "inactivity_expiration_enabled",
    "approval_required",
}


def _desired(spec: dict[str, Any], current_name: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": current_name,
        "ssh_enabled": bool(spec.get("sshEnabled", False)),
        "login_expiration_enabled": bool(spec.get("loginExpirationEnabled", False)),
        "inactivity_expiration_enabled": bool(spec.get("inactivityExpirationEnabled", False)),
    }
    if "approvalRequired" in spec:
        body["approval_required"] = bool(spec["approvalRequired"])
    return body


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    for k in REQUEST_KEYS:
        if k in desired and current.get(k) != desired.get(k):
            return True
    return False


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    peer_name = spec["peerRef"]
    nb_id = resolver.peer_id(peer_name)  # raises KeyError if peer not registered
    current = resolver.client.get(f"/peers/{nb_id}")

    desired = _desired(spec, current_name=current.get("name") or peer_name)
    if _needs_update(current, desired):
        resolver.client.put(f"/peers/{nb_id}", desired)
    return {"id": nb_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    """Delete removes the peer from NetBird (un-registers it).

    If the CR is simply orphaned (peer already gone), we let finalize succeed.
    """
    status = parent.get("status") or {}
    nb_id = status.get("id")
    if not nb_id:
        spec = parent.get("spec") or {}
        try:
            nb_id = resolver.peer_id(spec.get("peerRef", ""))
        except (KeyError, NetBirdError):
            return {}
    if not nb_id:
        return {}
    try:
        resolver.client.delete(f"/peers/{nb_id}")
    except NotFound:
        pass
    return {}
