"""Handler for arbitrary resources nested under a NetBird Network."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..netbird_client import NotFound, Resolver


def _network_name(spec: dict[str, Any]) -> str:
    ref = spec["networkRef"]
    return ref["name"] if isinstance(ref, Mapping) else str(ref)


def _desired(spec: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "description": spec.get("description", ""),
        "address": spec["address"],
        "enabled": bool(spec.get("enabled", True)),
        "groups": resolver.group_ids(spec.get("groupRefs") or []),
    }


def _find_by_name(resolver: Resolver, network_id: str, name: str) -> dict[str, Any] | None:
    for item in resolver.client.get(f"/networks/{network_id}/resources") or []:
        if item.get("name") == name:
            return item
    return None


def _group_ids(values: list[Any]) -> list[str]:
    return sorted(
        str(value.get("id") if isinstance(value, Mapping) else value)
        for value in values or []
    )


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    return any(
        [
            current.get("name") != desired["name"],
            (current.get("description") or "") != desired["description"],
            current.get("address") != desired["address"],
            bool(current.get("enabled", True)) != desired["enabled"],
            _group_ids(current.get("groups") or []) != sorted(desired["groups"]),
        ]
    )


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    network_id = resolver.network_id(_network_name(spec))
    desired = _desired(spec, resolver)
    nb_id = status.get("id")
    old_network_id = status.get("networkId")

    if nb_id and old_network_id and old_network_id != network_id:
        try:
            resolver.client.delete(f"/networks/{old_network_id}/resources/{nb_id}")
        except NotFound:
            pass
        nb_id = None

    current: dict[str, Any] | None = None
    if nb_id and old_network_id == network_id:
        try:
            current = resolver.client.get(f"/networks/{network_id}/resources/{nb_id}")
        except NotFound:
            current = None
    if current is None:
        current = _find_by_name(resolver, network_id, desired["name"])
    if current is None:
        created = resolver.client.post(f"/networks/{network_id}/resources", desired)
        return {"id": created["id"], "networkId": network_id}
    nb_id = current["id"]
    if _needs_update(current, desired):
        resolver.client.put(f"/networks/{network_id}/resources/{nb_id}", desired)
    return {"id": nb_id, "networkId": network_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    status = parent.get("status") or {}
    nb_id = status.get("id")
    network_id = status.get("networkId")
    if not (nb_id and network_id):
        return {}
    try:
        resolver.client.delete(f"/networks/{network_id}/resources/{nb_id}")
    except NotFound:
        pass
    return {}
