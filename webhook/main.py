"""FastAPI HTTP server — Metacontroller sync/finalize hook for NetBird CRDs.

Routes:
  POST /sync/{kind}      -> calls handler.sync(parent, resolver) -> status dict
  POST /finalize/{kind}  -> calls handler.finalize(parent, resolver) -> status dict

Singleton kinds (account-settings, dns-settings) reject parents whose
metadata.name is not "default".
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import handlers
from .netbird_client import NetBirdClient, NetBirdError, Resolver

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("netbird-complementary-operator")


SyncFn = Callable[[dict, Resolver], dict]

SINGLETON_KINDS = {"account-settings", "dns-settings", "instance"}

HANDLERS: dict[str, tuple[SyncFn, SyncFn]] = {
    "instance": (handlers.instance.sync_with_children, handlers.instance.finalize),
    "identity-sync": (handlers.identity_sync.sync, handlers.identity_sync.finalize),
    "account-settings": (handlers.account_settings.sync, handlers.account_settings.finalize),
    "dns-settings": (handlers.dns_settings.sync, handlers.dns_settings.finalize),
    "nameserver-group": (handlers.nameserver_group.sync, handlers.nameserver_group.finalize),
    "policy": (handlers.policy.sync, handlers.policy.finalize),
    "network": (handlers.network.sync, handlers.network.finalize),
    "network-resource": (handlers.network_resource.sync, handlers.network_resource.finalize),
    "network-router": (handlers.network_router.sync, handlers.network_router.finalize),
    "peer-group-binding": (handlers.peer_group_binding.sync, handlers.peer_group_binding.finalize),
    "peer-enrollment": (handlers.peer_enrollment.sync_with_children, handlers.peer_enrollment.finalize),
    "route": (handlers.route.sync, handlers.route.finalize),
    "role-binding": (handlers.role_binding.sync, handlers.role_binding.finalize),
    "posture-check": (handlers.posture_check.sync, handlers.posture_check.finalize),
    "token": (handlers.token.sync, handlers.token.finalize),
    "peer": (handlers.peer.sync, handlers.peer.finalize),
    "dns-zone": (handlers.dns_zone.sync, handlers.dns_zone.finalize),
    "dns-record": (handlers.dns_record.sync, handlers.dns_record.finalize),
}

# Kinds that emit Kubernetes child objects (returned in the `children` field
# of the sync response). All others return `children: []`.
CHILDREN_KINDS = {"instance", "peer-enrollment", "token"}


app = FastAPI(title="netbird-complementary-operator", version="1.4.0")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _condition_state(condition: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in condition.items() if key != "lastTransitionTime"}


def _status_state(status: dict[str, Any]) -> dict[str, Any]:
    stable = {key: value for key, value in status.items() if key != "lastSyncTime"}
    if "conditions" in stable:
        stable["conditions"] = [
            _condition_state(condition) for condition in stable.get("conditions") or []
        ]
    return stable


def _stabilize_status(parent: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    """Avoid status-only reconciliation loops while retaining real transitions."""
    previous = dict(parent.get("status") or {})
    result = dict(desired)
    now = _now_iso()

    previous_conditions = {
        condition.get("type"): condition
        for condition in previous.get("conditions") or []
        if condition.get("type")
    }
    conditions: list[dict[str, Any]] = []
    for desired_condition in result.get("conditions") or []:
        condition = dict(desired_condition)
        old = previous_conditions.get(condition.get("type"))
        if old and _condition_state(old) == _condition_state(condition):
            transition = old.get("lastTransitionTime")
        else:
            transition = None
        condition["lastTransitionTime"] = transition or now
        conditions.append(condition)
    if "conditions" in result:
        result["conditions"] = conditions

    # Handler timestamps describe an attempted reconciliation, not desired
    # state. Publish a new one only when some observable status actually
    # changes; otherwise return the parent status byte-for-byte.
    result.pop("lastSyncTime", None)
    if _status_state(result) == _status_state(previous):
        return previous
    result["lastSyncTime"] = now
    return result


def _error_status(parent: dict, reason: str, message: str) -> dict:
    prev = (parent.get("status") or {}) if isinstance(parent, dict) else {}
    status = dict(prev)
    status.update({
        "synced": False,
        "observedGeneration": (parent.get("metadata") or {}).get("generation"),
        "conditions": [
            {
                "type": "Synced",
                "status": "False",
                "reason": reason,
                "message": message[:2000],
            },
            {
                "type": "Ready",
                "status": "False",
                "reason": reason,
                "message": message[:2000],
            },
            {
                "type": "Degraded",
                "status": "True",
                "reason": reason,
                "message": message[:2000],
            },
        ],
    })
    return _stabilize_status(parent, status)


def _instance_error_status(parent: dict, reason: str, message: str) -> dict:
    status = _error_status(parent, reason, message)
    previous = parent.get("status") or {}
    for field in ("credentialRotation", "credentialRecovery"):
        if field in previous:
            status[field] = previous[field]
    status["phase"] = "Degraded" if reason == "RecoveryRequired" else "Progressing"
    previous_bootstrap = (
        ((parent.get("status") or {}).get("bootstrap") or {}).get("phase")
    )
    status["bootstrap"] = {
        "phase": (
            "BootstrapSealed"
            if previous_bootstrap == "BootstrapSealed"
            else "RecoveryRequired" if reason == "RecoveryRequired" else "BootstrapPending"
        )
    }
    generation = (parent.get("metadata") or {}).get("generation")
    for condition in status.get("conditions") or []:
        condition["observedGeneration"] = generation
    return status


def _preserve_children(kind: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    if kind not in CHILDREN_KINDS:
        return []
    preserved: list[dict[str, Any]] = []
    for object_map in (payload.get("children") or {}).values():
        if isinstance(object_map, dict):
            preserved.extend(child for child in object_map.values() if isinstance(child, dict))
    return preserved


def _api_error_reason(exc: NetBirdError) -> str:
    if exc.status == 401:
        return "InvalidPAT"
    if exc.status == 404:
        return "NetBirdNotFound"
    if exc.status == 429:
        return "RateLimited"
    if exc.status >= 500 or exc.status == 0:
        return "NetBirdUnavailable"
    return "NetBirdAPIError"


def _check_singleton(kind: str, parent: dict) -> None:
    if kind in SINGLETON_KINDS:
        name = (parent.get("metadata") or {}).get("name")
        if name != "default":
            raise HTTPException(
                status_code=400,
                detail=f"{kind} is a singleton — metadata.name must be 'default' (got {name!r})",
            )


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/readyz")
async def readyz() -> dict:
    ok = bool(os.environ.get("NB_MGMT_URL"))
    if not ok:
        raise HTTPException(status_code=503, detail="NB_MGMT_URL missing")
    return {"ok": True}


@app.post("/sync/{kind}")
def sync(kind: str, payload: dict[str, Any]) -> JSONResponse:
    parent = payload.get("parent") or {}
    log.info(
        "sync kind=%s name=%s ns=%s gen=%s",
        kind,
        (parent.get("metadata") or {}).get("name"),
        (parent.get("metadata") or {}).get("namespace"),
        (parent.get("metadata") or {}).get("generation"),
    )
    if kind not in HANDLERS:
        raise HTTPException(status_code=404, detail=f"unknown kind {kind!r}")
    _check_singleton(kind, parent)

    sync_fn, _ = HANDLERS[kind]
    try:
        children_out: list[dict] = []
        resync_after: int | None = None
        if kind == "instance":
            status, children_out, resync_after = handlers.instance.sync_with_children(
                parent, payload.get("children") or {}
            )
        elif kind == "token":
            client = NetBirdClient()
            resolver = Resolver(client)
            status, children_out = handlers.token.sync_with_children(
                parent, resolver, payload.get("children") or {}
            )
        elif kind == "peer-enrollment":
            client = NetBirdClient()
            resolver = Resolver(client)
            status, children_out = handlers.peer_enrollment.sync_with_children(
                parent,
                resolver,
                payload.get("children") or {},
                payload.get("related") or {},
            )
            resync_after = 10 if status.get("phase") in {"Pending", "Enrolling"} else 60
        elif kind == "role-binding":
            client = NetBirdClient()
            resolver = Resolver(client)
            status = handlers.role_binding.sync(parent, resolver, payload.get("related") or {})
        elif kind == "identity-sync":
            client = NetBirdClient()
            status = handlers.identity_sync.sync(parent, client, payload.get("related") or {})
            resync_after = int((parent.get("spec") or {}).get("pollIntervalSeconds", 60))
        else:
            client = NetBirdClient()
            resolver = Resolver(client)
            status = sync_fn(parent, resolver)
        status.setdefault("synced", True)
        status.setdefault("observedGeneration", (parent.get("metadata") or {}).get("generation"))
        status.setdefault(
            "conditions",
            [
                {
                    "type": "Synced",
                    "status": "True",
                    "reason": "Reconciled",
                }
            ],
        )
        status = _stabilize_status(parent, status)
        response: dict[str, Any] = {"status": status, "children": children_out}
        if resync_after is not None:
            response["resyncAfterSeconds"] = resync_after
        if kind == "role-binding":
            response["resyncAfterSeconds"] = int((parent.get("spec") or {}).get("pollIntervalSeconds", 60))
        return JSONResponse(response)
    except NetBirdError as exc:
        log.warning("netbird api error kind=%s err=%s", kind, exc)
        reason = _api_error_reason(exc)
        message = str(exc)
        if kind == "token" and exc.status == 401:
            expires_at = (parent.get("status") or {}).get("expiresAt")
            if expires_at:
                try:
                    expiration = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    if expiration <= dt.datetime.now(dt.timezone.utc):
                        reason = "BootstrapRequired"
                        message = f"Managed PAT expired at {expires_at}; bootstrap/break-glass is required. {exc}"
                except (TypeError, ValueError):
                    pass
        return JSONResponse({"status": _error_status(parent, reason, message), "children": _preserve_children(kind, payload)})
    except handlers.instance.InstanceError as exc:
        log.warning("instance reconciliation error reason=%s err=%s", exc.reason, exc)
        return JSONResponse(
            {
                "status": _instance_error_status(parent, exc.reason, str(exc)),
                "children": _preserve_children(kind, payload),
                "resyncAfterSeconds": 10,
            }
        )
    except KeyError as exc:
        log.warning("resolver miss kind=%s err=%s", kind, exc)
        return JSONResponse({"status": _error_status(parent, "RefNotFound", str(exc)), "children": _preserve_children(kind, payload)})
    except ValueError as exc:
        log.warning("invalid spec kind=%s err=%s", kind, exc)
        return JSONResponse({"status": _error_status(parent, "InvalidSpec", str(exc)), "children": _preserve_children(kind, payload)})
    except Exception as exc:
        log.exception("sync failed kind=%s", kind)
        return JSONResponse({"status": _error_status(parent, "InternalError", str(exc)), "children": _preserve_children(kind, payload)})


@app.post("/finalize/{kind}")
def finalize(kind: str, payload: dict[str, Any]) -> JSONResponse:
    parent = payload.get("parent") or {}
    log.info(
        "finalize kind=%s name=%s ns=%s",
        kind,
        (parent.get("metadata") or {}).get("name"),
        (parent.get("metadata") or {}).get("namespace"),
    )
    if kind not in HANDLERS:
        raise HTTPException(status_code=404, detail=f"unknown kind {kind!r}")

    _, fin_fn = HANDLERS[kind]
    try:
        if kind == "instance":
            status, children_out, finalized = handlers.instance.finalize(
                parent, payload.get("children") or {}
            )
            status = _stabilize_status(parent, status)
            return JSONResponse(
                {"status": status, "children": children_out, "finalized": finalized}
            )
        client = NetBirdClient()
        resolver = Resolver(client)
        if kind == "role-binding":
            status = handlers.role_binding.finalize(parent, resolver, payload.get("related") or {})
        elif kind == "identity-sync":
            status, finalized = handlers.identity_sync.finalize(parent, client)
            status = _stabilize_status(parent, status)
            return JSONResponse({"status": status, "children": [], "finalized": finalized})
        else:
            status = fin_fn(parent, resolver)
        status = _stabilize_status(parent, status)
        return JSONResponse({"status": status, "children": [], "finalized": True})
    except (NetBirdError, handlers.instance.InstanceError) as exc:
        log.warning("finalize netbird api error kind=%s err=%s", kind, exc)
        reason = exc.reason if isinstance(exc, handlers.instance.InstanceError) else "NetBirdAPIError"
        return JSONResponse(
            {
                "status": _error_status(parent, reason, str(exc)),
                "children": _preserve_children(kind, payload),
                "finalized": False,
            }
        )
    except Exception as exc:
        log.exception("finalize failed kind=%s", kind)
        return JSONResponse(
            {
                "status": _error_status(parent, "InternalError", str(exc)),
                "children": [],
                "finalized": False,
            }
        )


@app.post("/customize/role-binding")
async def customize_role_binding(request: Request) -> JSONResponse:
    """Watch all bindings cluster-wide for deterministic account-level conflicts."""
    await request.json()
    rule: dict[str, Any] = {
        "apiVersion": "netbird.io/v1alpha1",
        "resource": "netbirdrolebindings",
        "labelSelector": {},
    }
    return JSONResponse({"relatedResources": [rule]})


@app.post("/customize/identity-sync")
async def customize_identity_sync(request: Request) -> JSONResponse:
    """Observe only the credential Secret named by this identity source."""
    payload = await request.json()
    parent = payload.get("parent") or {}
    metadata = parent.get("metadata") or {}
    provider = (parent.get("spec") or {}).get("provider") or {}
    secret_ref = provider.get("credentialsSecretRef") or {}
    name = secret_ref.get("name")
    namespace = metadata.get("namespace")
    if not isinstance(name, str) or not name or not isinstance(namespace, str) or not namespace:
        return JSONResponse({"relatedResources": []})
    return JSONResponse(
        {
            "relatedResources": [
                {
                    "apiVersion": "v1",
                    "resource": "secrets",
                    "namespace": namespace,
                    "names": [name],
                }
            ]
        }
    )


@app.post("/customize/peer-enrollment")
async def customize_peer_enrollment(request: Request) -> JSONResponse:
    """Observe exactly the local workload named by an enroll-mode resource."""
    payload = await request.json()
    parent = payload.get("parent") or {}
    spec = parent.get("spec") or {}
    metadata = parent.get("metadata") or {}
    ref = spec.get("workloadRef") or {}
    if spec.get("mode") != "enroll":
        return JSONResponse({"relatedResources": []})
    mapping = {
        ("apps/v1", "Deployment"): "deployments",
        ("apps/v1", "StatefulSet"): "statefulsets",
        ("apps/v1", "DaemonSet"): "daemonsets",
    }
    resource = mapping.get((ref.get("apiVersion"), ref.get("kind")))
    name = ref.get("name")
    namespace = metadata.get("namespace")
    if not resource or not name or not namespace:
        return JSONResponse({"relatedResources": []})
    return JSONResponse(
        {
            "relatedResources": [
                {
                    "apiVersion": "apps/v1",
                    "resource": resource,
                    "namespace": namespace,
                    "names": [name],
                }
            ]
        }
    )
