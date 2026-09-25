"""Handler for NetBirdPostureCheck."""

from __future__ import annotations

from typing import Any

from ..netbird_client import NetBirdError, NotFound, Resolver


def _checks(spec_checks: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "nbVersionCheck" in spec_checks:
        c = spec_checks["nbVersionCheck"] or {}
        if c.get("minVersion"):
            out["nb_version_check"] = {"min_version": c["minVersion"]}

    if "osVersionCheck" in spec_checks:
        osv: dict[str, Any] = {}
        for os_key in ("android", "darwin", "ios"):
            e = (spec_checks["osVersionCheck"] or {}).get(os_key)
            if e and e.get("minVersion"):
                osv[os_key] = {"min_version": e["minVersion"]}
        for os_key in ("linux", "windows"):
            e = (spec_checks["osVersionCheck"] or {}).get(os_key)
            if e and e.get("minKernelVersion"):
                osv[os_key] = {"min_kernel_version": e["minKernelVersion"]}
        if osv:
            out["os_version_check"] = osv

    if "geoLocationCheck" in spec_checks:
        g = spec_checks["geoLocationCheck"] or {}
        locs = []
        for loc in g.get("locations") or []:
            item: dict[str, Any] = {"country_code": loc["countryCode"]}
            if loc.get("cityName"):
                item["city_name"] = loc["cityName"]
            locs.append(item)
        out["geo_location_check"] = {"action": g["action"], "locations": locs}

    if "peerNetworkRangeCheck" in spec_checks:
        r = spec_checks["peerNetworkRangeCheck"] or {}
        out["peer_network_range_check"] = {
            "action": r["action"],
            "ranges": list(r.get("ranges") or []),
        }

    if "processCheck" in spec_checks:
        procs = []
        for p in (spec_checks["processCheck"] or {}).get("processes") or []:
            item: dict[str, Any] = {}
            if p.get("linuxPath"):
                item["linux_path"] = p["linuxPath"]
            if p.get("macPath"):
                item["mac_path"] = p["macPath"]
            if p.get("windowsPath"):
                item["windows_path"] = p["windowsPath"]
            if item:
                procs.append(item)
        if procs:
            out["process_check"] = {"processes": procs}

    return out


def _desired(spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": spec["name"],
        "description": spec["description"],
        "checks": _checks(spec.get("checks") or {}),
    }


def _find_by_name(resolver: Resolver, name: str) -> dict[str, Any] | None:
    items = resolver.client.get("/posture-checks") or []
    for it in items:
        if it.get("name") == name:
            return it
    return None


def _normalize(obj: Any) -> Any:
    """Strip None / {} / [] so server-side defaults don't cause phantom diffs.

    The NetBird API returns fields inconsistently (sometimes null, sometimes
    missing, sometimes empty dict/list). Without normalization every resync
    would detect a "drift" and issue a PUT, causing log spam and unneeded load.
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            n = _normalize(v)
            if n is None:
                continue
            if isinstance(n, (dict, list)) and not n:
                continue
            out[k] = n
        return out
    if isinstance(obj, list):
        return [_normalize(v) for v in obj if _normalize(v) is not None]
    return obj


def _needs_update(current: dict[str, Any], desired: dict[str, Any]) -> bool:
    cur = _normalize(
        {"name": current.get("name"), "description": current.get("description"),
         "checks": current.get("checks") or {}}
    )
    des = _normalize(
        {"name": desired.get("name"), "description": desired.get("description"),
         "checks": desired.get("checks") or {}}
    )
    return cur != des


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    status = parent.get("status") or {}
    desired = _desired(spec)

    nb_id = status.get("id")
    current: dict[str, Any] | None = None
    if nb_id:
        try:
            current = resolver.client.get(f"/posture-checks/{nb_id}")
        except NotFound:
            current = None
    if current is None:
        current = _find_by_name(resolver, desired["name"])

    if current is None:
        created = resolver.client.post("/posture-checks", desired)
        return {"id": created["id"]}

    nb_id = current["id"]
    if _needs_update(current, desired):
        resolver.client.put(f"/posture-checks/{nb_id}", desired)
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
        resolver.client.delete(f"/posture-checks/{nb_id}")
    except NotFound:
        pass
    return {}
