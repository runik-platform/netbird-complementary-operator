"""Handler for NetBirdRoute.

Upstream RouteRequest has NO `name` field — use (description + network_id) to
identify. We match existing routes by (network_id, description) for idempotent
recovery when .status.id is missing.
"""

from __future__ import annotations

from typing import Any

from ..netbird_client import NetBirdError, NotFound, Resolver


REQUEST_KEYS_ALL = {
    "description",
    "network_id",
    "enabled",
    "peer",
    "peer_groups",
    "network",
    "domains",
    "metric",
    "masquerade",
    "groups",
    "keep_route",
    "access_control_groups",
    "skip_auto_apply",
}


def _desired(spec: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    peer_ref = spec.get("peerRef")
    peer_group_refs = spec.get("peerGroupRefs") or []
    if peer_ref and peer_group_refs:
        raise ValueError("peerRef and peerGroupRefs are mutually exclusive")
    network = spec.get("network")
    domains = spec.get("domains") or []
    if network and domains:
        raise ValueError("network and domains are mutually exclusive")

    body: dict[str, Any] = {
        "description": spec["description"],
        "network_id": spec["networkId"],
        "enabled": bool(spec.get("enabled", True)),
        "metric": int(spec.get("metric", 9999)),
        "masquerade": bool(spec.get("masquerade", True)),
        "groups": resolver.group_ids(spec.get("groupRefs") or []),
        "keep_route": bool(spec.get("keepRoute", False)),
    }

    if peer_ref:
        body["peer"] = resolver.peer_id(peer_ref)
    elif peer_group_refs:
        body["peer_groups"] = resolver.group_ids(peer_group_refs)

    if network:
        body["network"] = network
    elif domains:
        body["domains"] = list(domains)

    if spec.get("accessControlGroupRefs"):
        body["access_control_groups"] = resolver.group_ids(spec["accessControlGroupRefs"])
    if "skipAutoApply" in spec:
        body["skip_auto_apply"] = bool(spec["skipAutoApply"])

    return body


def _find_by_identity(resolver: Resolver, network_id: str, description: str) -> dict[str, Any] | None:
    items = resolver.client.get("/routes") or []
    for it in items:
        if it.get("network_id") == network_id and it.get("description") == description:
            return it
    return None


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    for k in REQUEST_KEYS_ALL:
        if k in desired and current.get(k) != desired.get(k):
            return True
        if k not in desired and current.get(k) not in (None, [], "", False):
            # Field present upstream but unset locally: leave alone if it's a
            # mutually-exclusive pair, otherwise treat absence as "no change".
            continue
    return False


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    desired = _desired(spec, resolver)

    nb_id = status.get("id")
    current: dict[str, Any] | None = None
    if nb_id:
        try:
            current = resolver.client.get(f"/routes/{nb_id}")
        except NotFound:
            current = None
    if current is None:
        current = _find_by_identity(resolver, desired["network_id"], desired["description"])

    if current is None:
        created = resolver.client.post("/routes", desired)
        return {"id": created["id"]}

    nb_id = current["id"]
    if _needs_update(current, desired):
        resolver.client.put(f"/routes/{nb_id}", desired)
    return {"id": nb_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    status = parent.get("status") or {}
    nb_id = status.get("id")
    if not nb_id:
        spec = parent.get("spec") or {}
        if spec.get("networkId") and spec.get("description"):
            try:
                hit = _find_by_identity(resolver, spec["networkId"], spec["description"])
                if hit:
                    nb_id = hit["id"]
            except NetBirdError:
                return {}
    if not nb_id:
        return {}
    try:
        resolver.client.delete(f"/routes/{nb_id}")
    except NotFound:
        pass
    return {}
