"""Handler for standalone NetBird Network containers."""

from __future__ import annotations

from typing import Any

from ..netbird_client import NotFound, Resolver


def _desired(spec: dict[str, Any]) -> dict[str, Any]:
    if not bool(spec.get("enabled", True)):
        raise ValueError(
            "NetBird /api/networks has no enabled state; enabled=false cannot be reconciled safely"
        )
    return {"name": spec["name"], "description": spec.get("description", "")}


def _find_by_name(resolver: Resolver, name: str) -> dict[str, Any] | None:
    for item in resolver.client.get("/networks") or []:
        if item.get("name") == name:
            return item
    return None


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    return current.get("name") != desired["name"] or (current.get("description") or "") != desired["description"]


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    desired = _desired(spec)
    nb_id = status.get("id")
    current: dict[str, Any] | None = None
    if nb_id:
        try:
            current = resolver.client.get(f"/networks/{nb_id}")
        except NotFound:
            current = None
    if current is None:
        current = _find_by_name(resolver, desired["name"])
    if current is None:
        created = resolver.client.post("/networks", desired)
        return {"id": created["id"]}
    nb_id = current["id"]
    if _needs_update(current, desired):
        resolver.client.put(f"/networks/{nb_id}", desired)
    return {"id": nb_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    nb_id = (parent.get("status") or {}).get("id")
    if not nb_id:
        return {}
    try:
        resolver.client.delete(f"/networks/{nb_id}")
    except NotFound:
        pass
    return {}
