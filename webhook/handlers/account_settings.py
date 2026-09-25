"""Handler for NetBirdAccountSettings (singleton)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..netbird_client import Resolver


def _get_account(resolver: Resolver) -> dict[str, Any]:
    accounts = resolver.client.get("/accounts") or []
    if not accounts:
        raise RuntimeError("NetBird returned no account")
    account = accounts[0]
    acc_id = account.get("id")
    if not acc_id:
        raise RuntimeError("NetBird account without id")
    return account


def _apply_spec(
    current: dict[str, Any], spec: dict[str, Any], resolver: Resolver
) -> dict[str, Any]:
    """Overlay the fields owned by the CR onto the complete API settings."""
    s = deepcopy(current)

    ple = spec.get("peerLoginExpiration") or {}
    if "enabled" in ple:
        s["peer_login_expiration_enabled"] = bool(ple["enabled"])
    if "seconds" in ple:
        s["peer_login_expiration"] = int(ple["seconds"])

    pie = spec.get("peerInactivityExpiration") or {}
    if "enabled" in pie:
        s["peer_inactivity_expiration_enabled"] = bool(pie["enabled"])
    if "seconds" in pie:
        s["peer_inactivity_expiration"] = int(pie["seconds"])

    if "regularUsersViewBlocked" in spec:
        s["regular_users_view_blocked"] = bool(spec["regularUsersViewBlocked"])
    if "groupsPropagationEnabled" in spec:
        s["groups_propagation_enabled"] = bool(spec["groupsPropagationEnabled"])

    jwt = spec.get("jwtGroups") or {}
    if "enabled" in jwt:
        s["jwt_groups_enabled"] = bool(jwt["enabled"])
    if "claimName" in jwt:
        s["jwt_groups_claim_name"] = str(jwt["claimName"])
    if "allowGroups" in jwt:
        s["jwt_allow_groups"] = list(jwt["allowGroups"] or [])

    if "routingPeerDnsResolutionEnabled" in spec:
        s["routing_peer_dns_resolution_enabled"] = bool(spec["routingPeerDnsResolutionEnabled"])
    if "dnsDomain" in spec:
        s["dns_domain"] = str(spec["dnsDomain"])
    if "networkRange" in spec:
        s["network_range"] = str(spec["networkRange"])

    pe = spec.get("peerExpose") or {}
    if "enabled" in pe:
        s["peer_expose_enabled"] = bool(pe["enabled"])
    if "groupRefs" in pe:
        s["peer_expose_groups"] = resolver.group_ids(pe["groupRefs"] or [])

    if "lazyConnectionEnabled" in spec:
        s["lazy_connection_enabled"] = bool(spec["lazyConnectionEnabled"])

    au = spec.get("autoUpdate") or {}
    if "version" in au:
        s["auto_update_version"] = str(au["version"])
    if "always" in au:
        s["auto_update_always"] = bool(au["always"])

    # NetBird requires all five nested fields when "extra" is present. Preserve
    # unspecified live values and only fall back to API-safe defaults when the
    # account does not have the field yet.
    if "extra" in spec:
        e = spec["extra"] or {}
        extra = deepcopy(s.get("extra") or {})
        field_map = {
            "peerApprovalEnabled": "peer_approval_enabled",
            "userApprovalRequired": "user_approval_required",
            "networkTrafficLogsEnabled": "network_traffic_logs_enabled",
            "networkTrafficPacketCounterEnabled": "network_traffic_packet_counter_enabled",
        }
        for spec_field, api_field in field_map.items():
            if spec_field in e:
                extra[api_field] = bool(e[spec_field])
        if "networkTrafficLogsGroupRefs" in e:
            extra["network_traffic_logs_groups"] = resolver.group_ids(
                e["networkTrafficLogsGroupRefs"] or []
            )
        extra.setdefault("peer_approval_enabled", False)
        extra.setdefault("user_approval_required", False)
        extra.setdefault("network_traffic_logs_enabled", False)
        extra.setdefault("network_traffic_logs_groups", [])
        extra.setdefault("network_traffic_packet_counter_enabled", False)
        s["extra"] = extra

    return s


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    account = _get_account(resolver)
    acc_id = account["id"]
    current = account.get("settings") or {}
    settings = _apply_spec(current, spec, resolver)
    if settings != current:
        resolver.client.put(f"/accounts/{acc_id}", {"settings": settings})
    return {"id": acc_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    # Account cannot be deleted from here — just release the CR.
    return {}
