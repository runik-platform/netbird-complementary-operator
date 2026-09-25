"""Reconcile complete Keycloak realm-role snapshots into the Runik NetBird API."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
from typing import Any

from ..keycloak_client import KeycloakClient, KeycloakError
from ..netbird_client import NetBirdClient, NetBirdError


class CredentialError(ValueError):
    """The referenced provider credential is not available or usable."""


class SnapshotError(ValueError):
    """A complete provider response is internally inconsistent."""


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _condition(kind: str, value: str, reason: str, message: str = "") -> dict[str, Any]:
    condition: dict[str, Any] = {"type": kind, "status": value, "reason": reason}
    if message:
        condition["message"] = message[:2000]
    return condition


def _previous_status(parent: dict[str, Any]) -> dict[str, Any]:
    return dict(parent.get("status") or {})


def _failure_status(
    parent: dict[str, Any],
    *,
    reason: str,
    message: str,
    provider_available: str,
    snapshot_valid: str,
) -> dict[str, Any]:
    status = _previous_status(parent)
    status.update(
        synced=False,
        observedGeneration=(parent.get("metadata") or {}).get("generation"),
        conflicts=[message[:1000]] if reason == "SnapshotConflict" else status.get("conflicts", []),
        conditions=[
            _condition("Ready", "False", reason, message),
            _condition("ProviderAvailable", provider_available, reason, message),
            _condition("SnapshotValid", snapshot_valid, reason, message),
            _condition("Reconciled", "False", reason, message),
        ],
    )
    return status


def _find_secret(related: dict[str, Any], namespace: str, name: str) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    for object_map in (related or {}).values():
        if not isinstance(object_map, dict):
            continue
        for obj in object_map.values():
            metadata = obj.get("metadata") if isinstance(obj, dict) else None
            if (
                isinstance(metadata, dict)
                and obj.get("kind") == "Secret"
                and metadata.get("namespace") == namespace
                and metadata.get("name") == name
            ):
                matches.append(obj)
    if len(matches) != 1:
        raise CredentialError(f"credentials Secret {namespace}/{name} was not observed")
    return matches[0]


def _secret_value(secret: dict[str, Any], key: str) -> str:
    encoded = (secret.get("data") or {}).get(key)
    if not isinstance(encoded, str) or not encoded:
        raise CredentialError(f"credentials Secret key {key!r} is missing")
    try:
        value = base64.b64decode(encoded, validate=True).decode("utf-8").strip()
    except (ValueError, UnicodeDecodeError) as exc:
        raise CredentialError(f"credentials Secret key {key!r} is invalid") from exc
    if not value:
        raise CredentialError(f"credentials Secret key {key!r} is empty")
    return value


def _display_name(user: dict[str, Any]) -> str:
    full_name = " ".join(
        str(value).strip() for value in (user.get("firstName"), user.get("lastName")) if value
    )
    return full_name or str(user.get("username") or "")


def _build_snapshot(
    parent: dict[str, Any], keycloak: KeycloakClient
) -> tuple[dict[str, Any], int]:
    spec = parent.get("spec") or {}
    role_names = list(spec.get("realmRoles") or [])
    groups: list[dict[str, Any]] = []
    users_by_id: dict[str, dict[str, Any]] = {}
    memberships_by_user: dict[str, set[str]] = {}
    group_ids: set[str] = set()
    group_names: set[str] = set()

    if not role_names or any(not isinstance(name, str) or not name.strip() for name in role_names):
        raise SnapshotError("realmRoles must contain at least one non-empty role name")
    if len(set(role_names)) != len(role_names):
        raise SnapshotError("realmRoles contains duplicate role names")

    for role_name in sorted(role_names):
        role = keycloak.realm_role(str(role_name))
        external_id = str(role["id"])
        group_name = str(role["name"])
        if not external_id or not group_name:
            raise SnapshotError(f"Keycloak realm role {role_name!r} is incomplete")
        if external_id in group_ids:
            raise SnapshotError("Keycloak returned the same stable ID for multiple realm roles")
        normalized_name = group_name.casefold()
        if normalized_name in group_names:
            raise SnapshotError("Keycloak returned duplicate realm role names")
        group_ids.add(external_id)
        group_names.add(normalized_name)
        groups.append(
            {
                "external_id": external_id,
                "name": group_name,
                "adopt_existing": bool(spec.get("adoptExistingJWTGroups", False)),
            }
        )
        for user in keycloak.realm_role_users(str(role_name)):
            for field in ("username", "email", "firstName", "lastName"):
                if field in user and user[field] is not None and not isinstance(user[field], str):
                    raise SnapshotError("Keycloak returned invalid user profile data")
            if "enabled" in user and not isinstance(user["enabled"], bool):
                raise SnapshotError("Keycloak returned an invalid user enabled state")
            subject = str(user["id"])
            previous = users_by_id.get(subject)
            identity_fields = ("username", "email", "firstName", "lastName", "enabled")
            if previous is not None and any(previous.get(field) != user.get(field) for field in identity_fields):
                raise SnapshotError("Keycloak returned inconsistent data for one user across realm roles")
            users_by_id[subject] = user
            if user.get("enabled") is False:
                continue
            memberships_by_user.setdefault(subject, set()).add(external_id)

    users_by_id = {
        subject: user for subject, user in users_by_id.items() if user.get("enabled") is not False
    }

    users = [
        {
            "issuer": keycloak.issuer,
            "subject": subject,
            "email": str(user.get("email") or ""),
            "name": _display_name(user),
            "adopt_existing": bool(spec.get("adoptExistingUsers", False)),
        }
        for subject, user in sorted(users_by_id.items())
    ]
    memberships = [
        {
            "issuer": keycloak.issuer,
            "subject": subject,
            "group_external_ids": sorted(memberships_by_user.get(subject, set())),
        }
        for subject in sorted(users_by_id)
    ]
    groups.sort(key=lambda group: group["external_id"])
    snapshot = {"users": users, "groups": groups, "memberships": memberships}
    revision = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    request = {
        "instance_id": str(spec.get("instanceId", "default")),
        "source": str(spec["source"]),
        "revision": revision,
        "complete": True,
        **snapshot,
    }
    membership_count = sum(len(item["group_external_ids"]) for item in memberships)
    return request, membership_count


def _validate_management_response(response: Any, source: str, revision: str) -> dict[str, Any]:
    if (
        not isinstance(response, dict)
        or response.get("source") != source
        or response.get("revision") != revision
        or not isinstance(response.get("changed"), bool)
    ):
        raise ValueError("Management returned a mismatched identity result")
    for field in ("users", "groups", "memberships"):
        items = response.get(field)
        external_ids: set[str] = set()
        if not isinstance(items, list) or any(
            not isinstance(item, dict)
            or not isinstance(item.get("external_id"), str) or not item["external_id"]
            or not isinstance(item.get("internal_id"), str) or not item["internal_id"]
            or not isinstance(item.get("action"), str) or not item["action"]
            for item in items
        ):
            raise ValueError(f"Management returned an incomplete {field} diff")
        for item in items:
            if item["external_id"] in external_ids:
                raise ValueError(f"Management returned a duplicate {field} diff")
            external_ids.add(item["external_id"])
    return response


def sync(
    parent: dict[str, Any],
    netbird: NetBirdClient,
    related: dict[str, Any] | None = None,
    *,
    keycloak_factory: type[KeycloakClient] = KeycloakClient,
) -> dict[str, Any]:
    spec = parent.get("spec") or {}
    metadata = parent.get("metadata") or {}
    provider = spec.get("provider") or {}
    secret_ref = provider.get("credentialsSecretRef") or {}
    try:
        secret = _find_secret(related or {}, str(metadata.get("namespace", "")), str(secret_ref["name"]))
        client_secret = _secret_value(secret, str(secret_ref["key"]))
        keycloak = keycloak_factory(
            str(provider["baseUrl"]),
            str(provider["realm"]),
            str(provider["clientId"]),
            client_secret,
        )
        request, membership_count = _build_snapshot(parent, keycloak)
    except KeycloakError as exc:
        invalid_snapshot = exc.reason in {"IncompleteProviderResponse", "RealmRoleNotFound"}
        return _failure_status(
            parent,
            reason=exc.reason,
            message=str(exc),
            provider_available="True" if invalid_snapshot else "False",
            snapshot_valid="False" if invalid_snapshot else "Unknown",
        )
    except CredentialError as exc:
        return _failure_status(
            parent,
            reason="ProviderCredentialsUnavailable",
            message=str(exc),
            provider_available="Unknown",
            snapshot_valid="Unknown",
        )
    except (KeyError, TypeError, SnapshotError, ValueError) as exc:
        return _failure_status(
            parent,
            reason="InvalidSnapshot",
            message=str(exc),
            provider_available="True",
            snapshot_valid="False",
        )

    try:
        response = _validate_management_response(
            netbird.post("/runik/identity/sync", request), request["source"], request["revision"]
        )
    except NetBirdError as exc:
        conflict = exc.status in (409, 412)
        rejected = exc.status == 422
        if conflict:
            reason = "SnapshotConflict"
        elif rejected:
            reason = "ManagementRejectedSnapshot"
        elif exc.status in (401, 403):
            reason = "ManagementUnauthorized"
        else:
            reason = "ManagementUnavailable"
        return _failure_status(
            parent,
            reason=reason,
            message=f"Management rejected the identity snapshot with HTTP {exc.status}",
            provider_available="True",
            snapshot_valid="False" if rejected else "True",
        )
    except ValueError as exc:
        return _failure_status(
            parent,
            reason="IncompleteManagementResponse",
            message=str(exc),
            provider_available="True",
            snapshot_valid="True",
        )

    users = response["users"]
    groups = response["groups"]
    previous = _previous_status(parent)
    last_success = previous.get("lastSuccessfulSync")
    if response.get("changed") or previous.get("lastCompleteSnapshot") != request["revision"]:
        last_success = _now_iso()
    status = {
        "observedGeneration": metadata.get("generation"),
        "synced": True,
        "source": request["source"],
        "usersObserved": len(request["users"]),
        "groupsObserved": len(request["groups"]),
        "membershipsApplied": membership_count,
        "lastCompleteSnapshot": request["revision"],
        "lastSuccessfulSync": last_success or _now_iso(),
        "users": users,
        "groups": groups,
        "conflicts": [],
        "retainedGroups": sorted(item["external_id"] for item in groups if item["action"] == "retained"),
        "disabledUsers": sorted(item["external_id"] for item in users if item["action"] == "disabled"),
        "conditions": [
            _condition("Ready", "True", "Reconciled"),
            _condition("ProviderAvailable", "True", "CompleteSnapshotFetched"),
            _condition("SnapshotValid", "True", "CompleteSnapshotValidated"),
            _condition("Reconciled", "True", "Applied"),
        ],
    }
    return status


def finalize(parent: dict[str, Any], netbird: NetBirdClient) -> tuple[dict[str, Any], bool]:
    spec = parent.get("spec") or {}
    if spec.get("deletionPolicy", "Retain") == "Retain":
        status = _previous_status(parent)
        status["conditions"] = [_condition("Reconciled", "True", "Retained", "Externally managed identities were retained")]
        return status, True

    metadata = parent.get("metadata") or {}
    revision = "deletion-" + hashlib.sha256(str(metadata.get("uid", "")).encode("utf-8")).hexdigest()
    request = {
        "instance_id": str(spec.get("instanceId", "default")),
        "source": str(spec["source"]),
        "revision": revision,
        "complete": True,
        "users": [],
        "groups": [],
        "memberships": [],
    }
    try:
        response = _validate_management_response(
            netbird.post("/runik/identity/sync", request), request["source"], revision
        )
    except NetBirdError as exc:
        return _failure_status(
            parent,
            reason="ManagementUnavailable",
            message=f"Management rejected the deletion snapshot with HTTP {exc.status}",
            provider_available="Unknown",
            snapshot_valid="True",
        ), False
    except ValueError as exc:
        return _failure_status(
            parent,
            reason="IncompleteManagementResponse",
            message=str(exc),
            provider_available="Unknown",
            snapshot_valid="True",
        ), False
    return {
        "source": request["source"],
        "disabledUsers": sorted(
            item["external_id"] for item in response["users"] if item["action"] == "disabled"
        ),
        "retainedGroups": sorted(
            item["external_id"] for item in response["groups"] if item["action"] == "retained"
        ),
        "conditions": [_condition("Reconciled", "True", "DisabledOnDelete")],
    }, True
