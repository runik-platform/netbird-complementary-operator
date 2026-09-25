"""Declarative external-group to NetBird role binding.

This handler deliberately only operates on users already returned by
``GET /users``.  ``NetBirdIdentitySync`` may preprovision those users and this
module never calls ``POST /users``.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Iterable
from typing import Any

from ..netbird_client import NetBirdError, Resolver

log = logging.getLogger(__name__)
APPLIED_ROLE = "admin"
PROTECTED_IDENTITY_NAME = "operator-owner"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _condition(kind: str, status: str, reason: str, message: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {
        "type": kind,
        "status": status,
        "reason": reason,
        "lastTransitionTime": _now_iso(),
    }
    if message:
        out["message"] = message[:2000]
    return out


def _binding_key(binding: dict[str, Any]) -> str:
    metadata = binding.get("metadata") or {}
    return f"{metadata.get('namespace', '')}/{metadata.get('name', '')}"


def _related_bindings(parent: dict[str, Any], related: dict[str, Any] | None) -> list[dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {_binding_key(parent): parent}
    for kind_map in (related or {}).values():
        if not isinstance(kind_map, dict):
            continue
        for item in kind_map.values():
            if isinstance(item, dict) and item.get("kind") == "NetBirdRoleBinding":
                bindings[_binding_key(item)] = item
    return [bindings[key] for key in sorted(bindings)]


def _managed_by_id(parent: dict[str, Any]) -> dict[str, dict[str, Any]]:
    managed: dict[str, dict[str, Any]] = {}
    for entry in (parent.get("status") or {}).get("managedUsers") or []:
        if entry.get("userId"):
            managed[str(entry["userId"])] = {
                "userId": str(entry["userId"]),
                "previousRole": str(entry.get("previousRole", "user")),
                "appliedRole": str(entry.get("appliedRole", APPLIED_ROLE)),
            }
    return managed


def _user_update_body(user: dict[str, Any], role: str) -> dict[str, Any]:
    # UserRequest requires all three fields in NetBird's supported OpenAPI.
    return {
        "role": role,
        "auto_groups": list(user.get("auto_groups") or []),
        "is_blocked": bool(user.get("is_blocked", False)),
    }


def _base_status(
    parent: dict[str, Any],
    *,
    matched: int,
    managed: Iterable[dict[str, Any]],
    promoted: int = 0,
    restored: int = 0,
    skipped: int = 0,
    conflicts: Iterable[str] = (),
) -> dict[str, Any]:
    return {
        "observedGeneration": (parent.get("metadata") or {}).get("generation"),
        "synced": True,
        "lastSyncTime": _now_iso(),
        "matchedUsers": matched,
        "promotedUsers": promoted,
        "restoredUsers": restored,
        "skippedUsers": skipped,
        "conflicts": sorted(set(conflicts)),
        "managedUsers": sorted(managed, key=lambda item: item["userId"]),
    }


def _update_failed_status(
    parent: dict[str, Any],
    matched: int,
    managed: Iterable[dict[str, Any]],
    exc: NetBirdError,
    *,
    promoted: int = 0,
    restored: int = 0,
    skipped: int = 0,
    conflicts: Iterable[str] = (),
) -> dict[str, Any]:
    status = _base_status(
        parent,
        matched=matched,
        managed=managed,
        promoted=promoted,
        restored=restored,
        skipped=skipped,
        conflicts=conflicts,
    )
    status.update(
        synced=False,
        conditions=[
            _condition("Ready", "False", "RoleUpdateFailed", str(exc)),
            _condition("Synced", "False", "RoleUpdateFailed", "No further role changes were attempted."),
            _condition("Degraded", "True", "RoleUpdateFailed", str(exc)),
        ],
    )
    return status


def _group_ids_for_bindings(
    bindings: list[dict[str, Any]], resolver: Resolver
) -> dict[str, str]:
    group_ids: dict[str, str] = {}
    for binding in bindings:
        if (binding.get("metadata") or {}).get("deletionTimestamp"):
            continue
        name = (((binding.get("spec") or {}).get("groupRef") or {}).get("name"))
        if not name:
            continue
        try:
            group_ids[_binding_key(binding)] = resolver.group_id(str(name))
        except KeyError:
            # A broken *other* binding is not a contender.  The caller checks
            # the current binding explicitly and reports GroupNotFound.
            continue
    return group_ids


def _contenders_for_user(
    user: dict[str, Any], bindings: list[dict[str, Any]], group_ids: dict[str, str]
) -> list[str]:
    if _is_protected_identity(user):
        return []
    auto_groups = set(user.get("auto_groups") or [])
    contenders: list[str] = []
    for binding in bindings:
        key = _binding_key(binding)
        spec = binding.get("spec") or {}
        if spec.get("role", APPLIED_ROLE) != APPLIED_ROLE:
            continue
        if user.get("is_service_user", False) and not spec.get("includeServiceUsers", False):
            continue
        group_id = group_ids.get(key)
        if group_id and group_id in auto_groups:
            contenders.append(key)
    return sorted(contenders)


def _validate_users(users: Any) -> list[dict[str, Any]]:
    if not isinstance(users, list):
        raise TypeError("GET /users returned a non-list response; refusing role changes")
    required = {"id", "name", "role", "auto_groups"}
    incomplete = [index for index, user in enumerate(users) if not isinstance(user, dict) or not required <= user.keys()]
    if incomplete:
        raise ValueError(
            f"GET /users returned incomplete entries at indexes {incomplete[:10]}; refusing role changes"
        )
    return users


def _is_protected_identity(user: dict[str, Any]) -> bool:
    return user.get("role") == "owner" or user.get("name") == PROTECTED_IDENTITY_NAME


def _foreign_ownership(
    user_id: str, current_key: str, bindings: Iterable[dict[str, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    owners: list[tuple[str, dict[str, Any]]] = []
    for binding in bindings:
        key = _binding_key(binding)
        if key == current_key:
            continue
        ownership = _managed_by_id(binding).get(user_id)
        if ownership:
            owners.append((key, ownership))
    return sorted(owners, key=lambda item: item[0])


def sync(
    parent: dict[str, Any], resolver: Resolver, related: dict[str, Any] | None = None
) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    current_key = _binding_key(parent)
    managed = _managed_by_id(parent)

    # Schema validation also enforces this; keep a runtime guard for objects
    # restored from older/unvalidated API servers.
    if spec.get("role", APPLIED_ROLE) != APPLIED_ROLE:
        status = _base_status(parent, matched=0, managed=managed.values())
        status.update(
            synced=False,
            conditions=[
                _condition("Ready", "False", "UnsupportedRole", "Only role 'admin' is supported; owner is never automated."),
                _condition("Synced", "False", "UnsupportedRole"),
                _condition("Degraded", "True", "UnsupportedRole"),
            ],
        )
        return status

    group_name = ((spec.get("groupRef") or {}).get("name"))
    try:
        own_group_id = resolver.group_id(str(group_name))
    except (TypeError, ValueError) as exc:
        status = _base_status(parent, matched=0, managed=managed.values())
        status.update(
            synced=False,
            conditions=[
                _condition("Ready", "False", "IncompleteResponse", str(exc)),
                _condition("Synced", "False", "IncompleteResponse", "No promotions or demotions were attempted."),
                _condition("Degraded", "True", "IncompleteResponse", str(exc)),
            ],
        )
        return status
    except KeyError:
        status = _base_status(parent, matched=0, managed=managed.values())
        status.update(
            synced=False,
            conditions=[
                _condition("Ready", "False", "GroupNotFound", f"NetBird group {group_name!r} does not exist; JWT group sync must create it first."),
                _condition("Synced", "False", "GroupNotFound"),
                _condition("Degraded", "True", "GroupNotFound"),
            ],
        )
        return status

    bindings = _related_bindings(parent, related)
    try:
        group_ids = _group_ids_for_bindings(bindings, resolver)
    except (TypeError, ValueError) as exc:
        status = _base_status(parent, matched=0, managed=managed.values())
        status.update(
            synced=False,
            conditions=[
                _condition("Ready", "False", "IncompleteResponse", str(exc)),
                _condition("Synced", "False", "IncompleteResponse", "No promotions or demotions were attempted."),
                _condition("Degraded", "True", "IncompleteResponse", str(exc)),
            ],
        )
        return status
    group_ids[current_key] = own_group_id
    try:
        users = _validate_users(resolver.client.get("/users"))
    except (TypeError, ValueError) as exc:
        status = _base_status(parent, matched=0, managed=managed.values())
        status.update(
            synced=False,
            conditions=[
                _condition("Ready", "False", "IncompleteResponse", str(exc)),
                _condition("Synced", "False", "IncompleteResponse", "No promotions or demotions were attempted."),
                _condition("Degraded", "True", "IncompleteResponse", str(exc)),
            ],
        )
        return status
    users_by_id = {str(user["id"]): user for user in users}
    members = [user for user in users if own_group_id in set(user.get("auto_groups") or [])]

    owner_skips: list[str] = []
    service_skips: list[str] = []
    already_admin_skips: list[str] = []
    conflicts: list[str] = []
    promoted_ids: set[str] = set()
    restored_ids: set[str] = set()

    for user in members:
        user_id = str(user["id"])
        if _is_protected_identity(user):
            owner_skips.append(user_id)
            managed.pop(user_id, None)
            continue
        if user.get("is_service_user", False) and not spec.get("includeServiceUsers", False):
            service_skips.append(user_id)
            continue

        contenders = _contenders_for_user(user, bindings, group_ids)
        winner = contenders[0] if contenders else current_key
        if winner != current_key:
            conflicts.append(f"{user_id} is owned by binding {winner}")
            # Keep ownership until the deterministic winner has copied it.
            # This prevents a reconcile-order race from turning a managed
            # admin into an unowned pre-existing admin.
            winner_binding = next(
                (binding for binding in bindings if _binding_key(binding) == winner), None
            )
            if winner_binding and user_id in _managed_by_id(winner_binding):
                managed.pop(user_id, None)
            continue

        if user_id in managed:
            if user.get("role") != managed[user_id]["appliedRole"]:
                try:
                    resolver.client.put(
                        f"/users/{user_id}", _user_update_body(user, APPLIED_ROLE)
                    )
                except NetBirdError as exc:
                    return _update_failed_status(
                        parent,
                        len(members),
                        managed.values(),
                        exc,
                        promoted=len(promoted_ids),
                        skipped=len(set(owner_skips + service_skips + already_admin_skips)),
                        conflicts=conflicts,
                    )
                user["role"] = APPLIED_ROLE
                promoted_ids.add(user_id)
                log.info("role binding=%s repaired role drift user=%s", current_key, user_id)
            continue

        if user.get("role") == APPLIED_ROLE:
            foreign = _foreign_ownership(user_id, current_key, bindings)
            if foreign:
                # Transfer the previous-role record to the lexical winner.
                # The former owner will release its status after observing
                # this claim, independent of controller reconcile order.
                managed[user_id] = dict(foreign[0][1])
                conflicts.append(
                    f"{user_id} ownership transferred from binding {foreign[0][0]}"
                )
                continue
            # A genuinely pre-existing admin is deliberately not claimed.
            already_admin_skips.append(user_id)
            continue

        previous_role = str(user.get("role") or "user")
        try:
            resolver.client.put(f"/users/{user_id}", _user_update_body(user, APPLIED_ROLE))
        except NetBirdError as exc:
            return _update_failed_status(
                parent,
                len(members),
                managed.values(),
                exc,
                promoted=len(promoted_ids),
                skipped=len(set(owner_skips + service_skips + already_admin_skips)),
                conflicts=conflicts,
            )
        user["role"] = APPLIED_ROLE
        promoted_ids.add(user_id)
        managed[user_id] = {
            "userId": user_id,
            "previousRole": previous_role,
            "appliedRole": APPLIED_ROLE,
        }
        log.info("role binding=%s promoted user=%s previous_role=%s", current_key, user_id, previous_role)

    if spec.get("revokeOnExit", True):
        member_ids = {
            str(user["id"])
            for user in members
            if not _is_protected_identity(user)
            and (spec.get("includeServiceUsers", False) or not user.get("is_service_user", False))
        }
        for user_id, ownership in list(managed.items()):
            if user_id in member_ids:
                continue
            user = users_by_id.get(user_id)
            if user is None:
                managed.pop(user_id, None)
                continue
            if _is_protected_identity(user):
                managed.pop(user_id, None)
                owner_skips.append(user_id)
                continue

            contenders = _contenders_for_user(user, bindings, group_ids)
            other_contenders = [key for key in contenders if key != current_key]
            if other_contenders:
                # Never demote while another binding still targets the user.
                conflicts.append(f"{user_id} also matches binding {other_contenders[0]}; restoration skipped")
                winner_binding = next(
                    (
                        binding
                        for binding in bindings
                        if _binding_key(binding) == other_contenders[0]
                    ),
                    None,
                )
                if winner_binding and user_id in _managed_by_id(winner_binding):
                    managed.pop(user_id, None)
                continue

            if user.get("role") != ownership["previousRole"]:
                try:
                    resolver.client.put(
                        f"/users/{user_id}", _user_update_body(user, ownership["previousRole"])
                    )
                except NetBirdError as exc:
                    return _update_failed_status(
                        parent,
                        len(members),
                        managed.values(),
                        exc,
                        promoted=len(promoted_ids),
                        restored=len(restored_ids),
                        skipped=len(set(owner_skips + service_skips + already_admin_skips)),
                        conflicts=conflicts,
                    )
            restored_ids.add(user_id)
            managed.pop(user_id, None)
            log.info("role binding=%s restored user=%s role=%s", current_key, user_id, ownership["previousRole"])

    messages: list[str] = []
    if not members:
        messages.append("No provisioned NetBird users currently match; a later complete identity snapshot will be observed on the next poll.")
    if owner_skips:
        messages.append(f"Protected Owner identities skipped: {', '.join(sorted(set(owner_skips)))}.")
    if service_skips:
        messages.append(f"Service users excluded: {', '.join(sorted(set(service_skips)))}.")
    if already_admin_skips:
        messages.append(f"Pre-existing admins left unowned: {', '.join(sorted(set(already_admin_skips)))}.")
    if conflicts:
        messages.append("Binding conflicts: " + "; ".join(sorted(set(conflicts))) + ".")
    message = " ".join(messages) or f"Reconciled {len(members)} existing member(s)."
    degraded = bool(conflicts)

    status = _base_status(
        parent,
        matched=len(members),
        managed=managed.values(),
        promoted=len(promoted_ids),
        restored=len(restored_ids),
        skipped=len(set(owner_skips + service_skips + already_admin_skips)),
        conflicts=conflicts,
    )
    status["conditions"] = [
        _condition("Ready", "True", "Reconciled", message),
        _condition("Synced", "True", "Reconciled", message),
        _condition("Degraded", "True" if degraded else "False", "Conflict" if degraded else "Healthy", message if degraded else ""),
    ]
    return status


def finalize(
    parent: dict[str, Any], resolver: Resolver, related: dict[str, Any] | None = None
) -> dict[str, Any]:
    managed = _managed_by_id(parent)
    if not managed or not (parent.get("spec") or {}).get("restoreOnDelete", True):
        return {"managedUsers": []}

    current_key = _binding_key(parent)
    try:
        users = _validate_users(resolver.client.get("/users"))
        users_by_id = {str(user["id"]): user for user in users}
        bindings = [binding for binding in _related_bindings(parent, related) if _binding_key(binding) != current_key]
        group_ids = _group_ids_for_bindings(bindings, resolver)

        skipped: list[str] = []
        for user_id, ownership in managed.items():
            user = users_by_id.get(user_id)
            if not user or _is_protected_identity(user):
                continue
            if user.get("role") != ownership["appliedRole"]:
                skipped.append(f"{user_id} changed manually")
                continue
            contenders = _contenders_for_user(user, bindings, group_ids)
            if contenders:
                skipped.append(f"{user_id} still matches {contenders[0]}")
                continue
            resolver.client.put(
                f"/users/{user_id}", _user_update_body(user, ownership["previousRole"])
            )
        message = "; ".join(skipped) if skipped else "Owned role assignments restored."
        return {
            "managedUsers": [],
            "conditions": [_condition("Synced", "True", "Finalized", message)],
        }
    except (NetBirdError, KeyError, TypeError, ValueError) as exc:
        # Restoration is best effort. Releasing the finalizer avoids a
        # permanent Terminating object during a NetBird outage; leaving an
        # admin role in place is safer than an uninformed demotion.
        log.error("role binding=%s restore-on-delete skipped: %s", current_key, exc)
        return {
            "managedUsers": [],
            "conditions": [
                _condition(
                    "Degraded",
                    "True",
                    "RestoreSkipped",
                    f"Could not safely restore roles; finalizer released to avoid permanent termination: {exc}",
                )
            ],
        }
