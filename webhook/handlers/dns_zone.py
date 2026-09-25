"""Handler for NetBirdDNSZone."""

from __future__ import annotations

from typing import Any

from ..netbird_client import NetBirdError, NotFound, Resolver


REQUEST_KEYS = {
    "name",
    "domain",
    "enabled",
    "enable_search_domain",
    "distribution_groups",
}


def _desired(spec: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "domain": spec["domain"],
        "enabled": bool(spec.get("enabled", True)),
        "enable_search_domain": bool(spec.get("enableSearchDomain", False)),
        "distribution_groups": resolver.group_ids(spec.get("distributionGroupRefs") or []),
    }


def _find_by_name(resolver: Resolver, name: str) -> dict[str, Any] | None:
    items = resolver.client.get("/dns/zones") or []
    for it in items:
        if it.get("name") == name:
            return it
    return None


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    for k in REQUEST_KEYS:
        cur = current.get(k)
        des = desired.get(k)
        if k == "distribution_groups":
            cur = sorted(cur or [])
            des = sorted(des or [])
        if cur != des:
            return True
    return False


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    desired = _desired(spec, resolver)

    nb_id = status.get("id")
    current: dict[str, Any] | None = None
    if nb_id:
        try:
            current = resolver.client.get(f"/dns/zones/{nb_id}")
        except NotFound:
            current = None
    if current is None:
        current = _find_by_name(resolver, desired["name"])

    if current is None:
        created = resolver.client.post("/dns/zones", desired)
        return {"id": created["id"]}

    nb_id = current["id"]
    if _needs_update(current, desired):
        resolver.client.put(f"/dns/zones/{nb_id}", desired)
    return {"id": nb_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    status = parent.get("status") or {}
    nb_id = status.get("id")
    if not nb_id:
        name = ((parent.get("spec") or {}).get("name"))
        if name:
            try:
                hit = _find_by_name(resolver, name)
                if hit:
                    nb_id = hit["id"]
            except NetBirdError:
                return {}
    if not nb_id:
        return {}
    try:
        resolver.client.delete(f"/dns/zones/{nb_id}")
    except NotFound:
        pass
    return {}
