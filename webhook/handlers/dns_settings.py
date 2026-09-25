"""Handler for NetBirdDNSSettings (singleton)."""

from __future__ import annotations

from typing import Any

from ..netbird_client import Resolver


def _desired_body(spec: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    refs = spec.get("disabledManagementGroupRefs") or []
    return {"disabled_management_groups": resolver.group_ids(refs)}


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    desired = _desired_body(spec, resolver)
    current = resolver.client.get("/dns/settings") or {}
    if current.get("disabled_management_groups") != desired["disabled_management_groups"]:
        resolver.client.put("/dns/settings", desired)
    return {}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    # DNS settings cannot be deleted; nothing to do.
    return {}
