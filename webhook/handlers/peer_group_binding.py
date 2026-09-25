"""Handler for ownership of one peer-to-group membership edge."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..netbird_client import NotFound, Resolver


def _ids(values: list[Any]) -> list[str]:
    return [
        str(value.get("id") if isinstance(value, Mapping) else value)
        for value in values or []
        if (value.get("id") if isinstance(value, Mapping) else value)
    ]


def _resources(values: list[Any]) -> list[dict[str, str]]:
    resources: list[dict[str, str]] = []
    for value in values or []:
        if not isinstance(value, Mapping) or not value.get("id") or not value.get("type"):
            continue
        resources.append({"id": str(value["id"]), "type": str(value["type"])})
    return resources


def _set_membership(
    resolver: Resolver, group_id: str, peer_id: str, present: bool
) -> bool:
    group = resolver.client.get(f"/groups/{group_id}")
    peers = _ids(group.get("peers") or [])
    currently_present = peer_id in peers
    if currently_present == present:
        return False
    if present:
        peers.append(peer_id)
    else:
        peers = [item for item in peers if item != peer_id]
    resolver.client.put(
        f"/groups/{group_id}",
        {
            "name": group["name"],
            "peers": peers,
            "resources": _resources(group.get("resources") or []),
        },
    )
    return True


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    group_id = resolver.group_id(spec["groupRef"])
    peer_id = resolver.peer_id(spec["peerRef"])

    old_group_id = status.get("groupId")
    old_peer_id = status.get("peerId")
    if old_group_id and old_peer_id and (old_group_id, old_peer_id) != (group_id, peer_id):
        try:
            _set_membership(resolver, old_group_id, old_peer_id, False)
        except NotFound:
            pass

    _set_membership(resolver, group_id, peer_id, True)
    return {"groupId": group_id, "peerId": peer_id, "managed": True}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    status = parent.get("status") or {}
    group_id = status.get("groupId")
    peer_id = status.get("peerId")
    if not (group_id and peer_id and status.get("managed")):
        return {}
    try:
        _set_membership(resolver, group_id, peer_id, False)
    except NotFound:
        pass
    return {}
