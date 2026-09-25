"""Handler for NetBirdNameserverGroup."""

from __future__ import annotations

from typing import Any

from ..netbird_client import NetBirdError, NotFound, Resolver


REQUEST_KEYS = {
    "name",
    "description",
    "nameservers",
    "enabled",
    "groups",
    "primary",
    "domains",
    "search_domains_enabled",
}


def _desired(spec: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "description": spec.get("description", ""),
        "nameservers": [
            {"ip": ns["ip"], "ns_type": ns["nsType"], "port": int(ns["port"])}
            for ns in spec.get("nameservers") or []
        ],
        "enabled": bool(spec.get("enabled", True)),
        "groups": resolver.group_ids(spec.get("groupRefs") or []),
        "primary": bool(spec.get("primary", True)),
        "domains": list(spec.get("domains") or []),
        "search_domains_enabled": bool(spec.get("searchDomainsEnabled", False)),
    }


def _find_by_name(resolver: Resolver, name: str) -> dict[str, Any] | None:
    items = resolver.client.get("/dns/nameservers") or []
    for it in items:
        if it.get("name") == name:
            return it
    return None


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    for k in REQUEST_KEYS:
        if current.get(k) != desired.get(k):
            return True
    return False


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = (parent.get("status") or {})
    desired = _desired(spec, resolver)

    existing_id = status.get("id")
    current: dict[str, Any] | None = None
    if existing_id:
        try:
            current = resolver.client.get(f"/dns/nameservers/{existing_id}")
        except NotFound:
            current = None
    if current is None:
        current = _find_by_name(resolver, desired["name"])

    if current is None:
        created = resolver.client.post("/dns/nameservers", desired)
        return {"id": created["id"]}

    nb_id = current["id"]
    if _needs_update(current, desired):
        resolver.client.put(f"/dns/nameservers/{nb_id}", desired)
    return {"id": nb_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    status = parent.get("status") or {}
    nb_id = status.get("id")
    if not nb_id:
        # Best-effort recovery lookup. If the API is unreachable (auth failure,
        # network partition) we still release the finalizer — otherwise a
        # transient outage traps the CR forever.
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
        resolver.client.delete(f"/dns/nameservers/{nb_id}")
    except NotFound:
        pass
    return {}
