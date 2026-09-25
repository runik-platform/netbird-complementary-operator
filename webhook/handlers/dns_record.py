"""Handler for NetBirdDNSRecord (nested under /api/dns/zones/{zid}/records)."""

from __future__ import annotations

from typing import Any

from ..netbird_client import NetBirdError, NotFound, Resolver


REQUEST_KEYS = {"name", "type", "content", "ttl"}


def _desired(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "type": spec["type"],
        "content": spec["content"],
        "ttl": int(spec["ttl"]),
    }


def _list(resolver: Resolver, zone_id: str) -> list[dict[str, Any]]:
    return resolver.client.get(f"/dns/zones/{zone_id}/records") or []


def _find(resolver: Resolver, zone_id: str, name: str, rtype: str) -> dict[str, Any] | None:
    for r in _list(resolver, zone_id):
        if r.get("name") == name and r.get("type") == rtype:
            return r
    return None


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    for k in REQUEST_KEYS:
        if current.get(k) != desired.get(k):
            return True
    return False


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    desired = _desired(spec)
    zone_id = resolver.dns_zone_id(spec["zoneRef"])

    nb_id = status.get("id")
    current: dict[str, Any] | None = None
    if nb_id and status.get("zoneId") == zone_id:
        try:
            current = resolver.client.get(f"/dns/zones/{zone_id}/records/{nb_id}")
        except NotFound:
            current = None
    if current is None:
        current = _find(resolver, zone_id, desired["name"], desired["type"])

    if current is None:
        created = resolver.client.post(f"/dns/zones/{zone_id}/records", desired)
        return {"id": created["id"], "zoneId": zone_id}

    nb_id = current["id"]
    if _needs_update(current, desired):
        resolver.client.put(f"/dns/zones/{zone_id}/records/{nb_id}", desired)
    return {"id": nb_id, "zoneId": zone_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    status = parent.get("status") or {}
    nb_id = status.get("id")
    zone_id = status.get("zoneId")
    if not (nb_id and zone_id):
        spec = parent.get("spec") or {}
        try:
            zone_id = resolver.dns_zone_id(spec.get("zoneRef", ""))
            hit = _find(resolver, zone_id, spec.get("name", ""), spec.get("type", ""))
            if hit:
                nb_id = hit["id"]
        except (KeyError, NetBirdError):
            return {}
    if not (nb_id and zone_id):
        return {}
    try:
        resolver.client.delete(f"/dns/zones/{zone_id}/records/{nb_id}")
    except NotFound:
        pass
    return {}
