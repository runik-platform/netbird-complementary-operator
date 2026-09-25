"""Handler for metadata-only routers nested under a NetBird Network."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..netbird_client import NotFound, Resolver


def _network_name(spec: dict[str, Any]) -> str:
    ref = spec["networkRef"]
    return ref["name"] if isinstance(ref, Mapping) else str(ref)


def _desired(spec: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    peer_ref = spec.get("peerRef")
    peer_group_refs = spec.get("peerGroupRefs") or []
    if peer_ref and peer_group_refs:
        raise ValueError("peerRef and peerGroupRefs are mutually exclusive")
    if not peer_ref and not peer_group_refs:
        raise ValueError("either peerRef or peerGroupRefs must be set")
    body: dict[str, Any] = {
        "enabled": bool(spec.get("enabled", True)),
        "masquerade": bool(spec.get("masquerade", True)),
        "metric": int(spec.get("metric", 9999)),
    }
    if peer_ref:
        body["peer"] = resolver.peer_id(peer_ref)
    else:
        body["peer_groups"] = resolver.group_ids(peer_group_refs)
    return body


def _identity_matches(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    if desired.get("peer"):
        return current.get("peer") == desired["peer"]
    return sorted(current.get("peer_groups") or []) == sorted(desired.get("peer_groups") or [])


def _find(resolver: Resolver, network_id: str, desired: dict[str, Any]) -> dict[str, Any] | None:
    for item in resolver.client.get(f"/networks/{network_id}/routers") or []:
        if _identity_matches(item, desired):
            return item
    return None


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    if (current.get("peer") or None) != (desired.get("peer") or None):
        return True
    for key in ("metric", "masquerade", "enabled"):
        if current.get(key) != desired.get(key):
            return True
    return sorted(current.get("peer_groups") or []) != sorted(desired.get("peer_groups") or [])


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    network_id = resolver.network_id(_network_name(spec))
    desired = _desired(spec, resolver)
    nb_id = status.get("id")
    old_network_id = status.get("networkId")

    if nb_id and old_network_id and old_network_id != network_id:
        try:
            resolver.client.delete(f"/networks/{old_network_id}/routers/{nb_id}")
        except NotFound:
            pass
        nb_id = None

    current: dict[str, Any] | None = None
    if nb_id and old_network_id == network_id:
        try:
            current = resolver.client.get(f"/networks/{network_id}/routers/{nb_id}")
        except NotFound:
            current = None
    if current is None:
        current = _find(resolver, network_id, desired)
    if current is None:
        created = resolver.client.post(f"/networks/{network_id}/routers", desired)
        return {"id": created["id"], "networkId": network_id}
    nb_id = current["id"]
    if _needs_update(current, desired):
        resolver.client.put(f"/networks/{network_id}/routers/{nb_id}", desired)
    return {"id": nb_id, "networkId": network_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    status = parent.get("status") or {}
    nb_id = status.get("id")
    network_id = status.get("networkId")
    if not (nb_id and network_id):
        return {}
    try:
        resolver.client.delete(f"/networks/{network_id}/routers/{nb_id}")
    except NotFound:
        pass
    return {}
