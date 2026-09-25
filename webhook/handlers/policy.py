"""Handler for full-fidelity NetBird access policies."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any

from ..netbird_client import NetBirdError, NotFound, Resolver


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _id(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("id") or "")
    return str(value or "")


def _desired(
    spec: dict[str, Any], resolver: Resolver, current: dict[str, Any] | None = None
) -> dict[str, Any]:
    for rule_spec in spec.get("rules") or []:
        ports = rule_spec.get("ports") or []
        port_ranges = rule_spec.get("portRanges") or []
        if ports and port_ranges:
            raise ValueError("policy rule ports and portRanges are mutually exclusive")
        protocol = rule_spec.get("protocol", "all")
        if protocol in {"all", "icmp"} and (ports or port_ranges):
            raise ValueError(f"policy rule protocol {protocol!r} cannot declare ports or portRanges")

    sources = resolver.group_ids(spec.get("sourceGroupRefs") or [])
    destinations = resolver.group_ids(spec.get("destinationGroupRefs") or [])
    posture_checks = resolver.posture_check_ids(spec.get("sourcePostureCheckRefs") or [])
    current_rule_ids = {
        rule.get("name"): rule.get("id")
        for rule in (current or {}).get("rules") or []
        if isinstance(rule, Mapping) and rule.get("name") and rule.get("id")
    }

    rules: list[dict[str, Any]] = []
    for rule_spec in spec.get("rules") or []:
        ports = rule_spec.get("ports") or []
        port_ranges = rule_spec.get("portRanges") or []
        protocol = rule_spec.get("protocol", "all")

        rule: dict[str, Any] = {
            "name": rule_spec["name"],
            "enabled": bool(rule_spec.get("enabled", True)),
            "action": rule_spec.get("action", "accept"),
            "bidirectional": bool(rule_spec.get("bidirectional", True)),
            "protocol": protocol,
            "sources": sources,
            "destinations": destinations,
        }
        if ports:
            rule["ports"] = [str(port) for port in ports]
        if port_ranges:
            rule["port_ranges"] = [
                {"start": int(port_range["start"]), "end": int(port_range["end"])}
                for port_range in port_ranges
            ]
        if rule["name"] in current_rule_ids:
            rule["id"] = current_rule_ids[rule["name"]]
        rules.append(rule)

    return {
        "name": spec["name"],
        "description": spec.get("description", ""),
        "enabled": bool(spec.get("enabled", True)),
        "source_posture_checks": posture_checks,
        "rules": rules,
    }


def _find_by_name(resolver: Resolver, name: str) -> dict[str, Any] | None:
    for item in resolver.client.get("/policies") or []:
        if item.get("name") == name:
            return item
    return None


def _normalized_rule(rule: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": rule.get("name") or "",
        "enabled": bool(rule.get("enabled", True)),
        "action": rule.get("action") or "accept",
        "bidirectional": bool(rule.get("bidirectional", True)),
        "protocol": rule.get("protocol") or "all",
        "sources": sorted(filter(None, (_id(value) for value in rule.get("sources") or []))),
        "destinations": sorted(
            filter(None, (_id(value) for value in rule.get("destinations") or []))
        ),
        "ports": sorted(str(port) for port in rule.get("ports") or []),
        "port_ranges": sorted(
            (int(item["start"]), int(item["end"]))
            for item in rule.get("port_ranges") or []
        ),
    }


def _normalized_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "name": policy.get("name") or "",
        "description": policy.get("description") or "",
        "enabled": bool(policy.get("enabled", True)),
        "source_posture_checks": sorted(
            filter(None, (_id(value) for value in policy.get("source_posture_checks") or []))
        ),
        "rules": sorted(
            (_normalized_rule(rule) for rule in policy.get("rules") or []),
            key=lambda rule: rule["name"],
        ),
    }


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    return _normalized_policy(current) != _normalized_policy(desired)


def _pending_delete_status(nb_id: str) -> dict[str, Any]:
    now = _now_iso()
    message = "Existing policy adopted; deletion will occur after its ID is persisted in status"
    return {
        "id": nb_id,
        "pendingDeletion": True,
        "synced": False,
        "conditions": [
            {
                "type": "Synced",
                "status": "False",
                "reason": "AdoptedPendingDeletion",
                "message": message,
                "lastTransitionTime": now,
            }
        ],
    }


def _sync_absent(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    nb_id = status.get("id")
    if not nb_id:
        current = _find_by_name(resolver, spec["name"])
        if current:
            return _pending_delete_status(current["id"])
        return {"state": "Absent"}

    try:
        resolver.client.delete(f"/policies/{nb_id}")
    except NotFound:
        pass
    return {"state": "Absent"}


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    if spec.get("state", "present") == "absent":
        return _sync_absent(parent, resolver)

    status = parent.get("status") or {}
    nb_id = status.get("id")
    current: dict[str, Any] | None = None
    if nb_id:
        try:
            current = resolver.client.get(f"/policies/{nb_id}")
        except NotFound:
            current = None
    if current is None:
        current = _find_by_name(resolver, spec["name"])

    desired = _desired(spec, resolver, current)
    if current is None:
        created = resolver.client.post("/policies", desired)
        return {"id": created["id"]}

    nb_id = current["id"]
    if _needs_update(current, desired):
        resolver.client.put(f"/policies/{nb_id}", desired)
    return {"id": nb_id}


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    nb_id = (parent.get("status") or {}).get("id")
    if not nb_id:
        return {}
    try:
        resolver.client.delete(f"/policies/{nb_id}")
    except NotFound:
        pass
    return {}
