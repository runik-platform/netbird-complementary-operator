"""NetBirdToken handler with two-phase PAT rotation.

The new PAT is created and published to the child Secret first. The old PAT
is deliberately left to expire naturally after the newly returned Secret
value is observed, avoiding a kubelet projection propagation race.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
from typing import Any

from ..netbird_client import NetBirdError, NotFound, Resolver

log = logging.getLogger(__name__)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime | None = None) -> str:
    return (value or _now()).astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _condition(kind: str, status: str, reason: str, message: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": kind,
        "status": status,
        "reason": reason,
        "lastTransitionTime": _iso(),
    }
    if message:
        result["message"] = message[:2000]
    return result


def _child_secret(name: str, key: str, value_b64: str) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name},
        "type": "Opaque",
        "data": {key: value_b64},
    }


def _existing_child_secret(children: dict[str, Any] | None, name: str) -> dict[str, Any] | None:
    if not children:
        return None
    by_kind = children.get("Secret.v1") or children.get("Secret.") or {}
    return by_kind.get(name)


def _value_hash(value_b64: str) -> str:
    return hashlib.sha256(value_b64.encode()).hexdigest()


def _status(
    *,
    token_id: str,
    user_id: str,
    expires_at: str | None,
    last_rotation: str | None,
    previous_token_id: str | None = None,
    pending_secret_hash: str | None = None,
    synced: bool = True,
    conditions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": token_id,
        "activeTokenId": token_id,
        "userId": user_id,
        "synced": synced,
    }
    if expires_at:
        result["expiresAt"] = expires_at
    if last_rotation:
        result["lastRotationTime"] = last_rotation
    if previous_token_id:
        result["previousTokenId"] = previous_token_id
    if pending_secret_hash:
        result["pendingSecretHash"] = pending_secret_hash
    result["conditions"] = conditions or [
        _condition("Ready", "True", "Valid"),
        _condition("Expiring", "False", "OutsideRenewalWindow"),
    ]
    return result


def _create_token(spec: dict[str, Any], resolver: Resolver, user_id: str) -> tuple[dict[str, Any], str]:
    body = {"name": spec["name"], "expires_in": int(spec.get("expiresInDays", 90))}
    generated = resolver.client.post(f"/users/{user_id}/tokens", body)
    pat = generated["personal_access_token"]
    plaintext_b64 = base64.b64encode(generated["plain_token"].encode()).decode()
    return pat, plaintext_b64


def sync_with_children(
    parent: dict[str, Any],
    resolver: Resolver,
    children: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    spec = parent.get("spec") or {}
    old_status = parent.get("status") or {}
    secret_name = spec["secretRef"]["name"]
    secret_key = spec["secretRef"].get("key", "token")
    expires_in_days = int(spec.get("expiresInDays", 90))
    # Older CRs allowed a one-day lifetime and have no renewal field. Keep
    # those objects valid; their effective window is zero rather than causing
    # an endless rotate-on-every-poll loop.
    renew_before_days = int(
        spec["renewBeforeDays"]
        if "renewBeforeDays" in spec
        else min(7, max(0, expires_in_days - 1))
    )

    user_id = resolver.user_id(spec["userRef"])
    existing_secret = _existing_child_secret(children, secret_name)
    existing_value = ((existing_secret or {}).get("data") or {}).get(secret_key)

    active_id = old_status.get("activeTokenId") or old_status.get("id")
    active_token: dict[str, Any] | None = None
    if active_id:
        try:
            active_token = resolver.client.get(f"/users/{user_id}/tokens/{active_id}")
        except NotFound:
            active_token = None

    previous_id = old_status.get("previousTokenId")
    pending_hash = old_status.get("pendingSecretHash")
    if previous_id and pending_hash and existing_value and _value_hash(existing_value) == pending_hash:
        log.info(
            "PAT rotation published; previous token id=%s will expire naturally",
            previous_id,
        )
        previous_id = None
        pending_hash = None

    expires_at = (active_token or {}).get("expiration_date") or old_status.get("expiresAt")
    if active_token is not None and not expires_at:
        message = "NetBird returned an active PAT without expiration_date; refusing rotation."
        return (
            _status(
                token_id=str(active_id), user_id=user_id, expires_at=None,
                last_rotation=old_status.get("lastRotationTime"), previous_token_id=previous_id,
                pending_secret_hash=pending_hash, synced=False,
                conditions=[
                    _condition("Ready", "False", "IncompleteResponse", message),
                    _condition("Expiring", "True", "ExpirationUnknown", message),
                    _condition("Degraded", "True", "IncompleteResponse", message),
                ],
            ),
            [existing_secret] if existing_secret else [],
        )
    expiration = _parse_time(expires_at)
    now = _now()

    if active_token is not None and expiration is not None and expiration <= now:
        message = (
            f"PAT {active_id} expired at {expires_at}. Automatic rotation is no longer safe; "
            "bootstrap a new administrative service-user PAT using the documented break-glass procedure."
        )
        log.error(message)
        return (
            _status(
                token_id=str(active_id), user_id=user_id, expires_at=expires_at,
                last_rotation=old_status.get("lastRotationTime"), previous_token_id=previous_id,
                pending_secret_hash=pending_hash, synced=False,
                conditions=[
                    _condition("Ready", "False", "TokenExpired", message),
                    _condition("Expiring", "True", "TokenExpired", message),
                    _condition("Degraded", "True", "BootstrapRequired", message),
                ],
            ),
            [existing_secret] if existing_secret else [],
        )

    secret_present = bool(existing_value)
    due = bool(
        active_token is not None
        and expiration is not None
        and expiration - now <= dt.timedelta(days=renew_before_days)
    )
    needs_new_token = active_token is None or not secret_present or due
    if previous_id and pending_hash:
        needs_new_token = False

    if needs_new_token:
        pat, new_value = _create_token(spec, resolver, user_id)
        new_id = str(pat["id"])
        new_expiration = pat.get("expiration_date")
        previous = str(active_id) if active_token is not None and active_id != new_id else None
        reason = "RenewedBeforeExpiry" if due else "Created"
        message = (
            f"Published new PAT {new_id}; previous PAT {previous} remains valid until the Secret update is observed."
            if previous else f"Published new PAT {new_id}."
        )
        log.info("PAT %s token=%s previous=%s expires=%s", reason, new_id, previous, new_expiration)
        return (
            _status(
                token_id=new_id, user_id=user_id, expires_at=new_expiration,
                last_rotation=_iso(now), previous_token_id=previous,
                pending_secret_hash=_value_hash(new_value) if previous else None,
                conditions=[
                    _condition("Ready", "True", reason, message),
                    _condition("Expiring", "False", "Rotated", message),
                ],
            ),
            [_child_secret(secret_name, secret_key, new_value)],
        )

    conditions = [
        _condition("Ready", "True", "Valid"),
        _condition("Expiring", "False", "OutsideRenewalWindow"),
    ]
    return (
        _status(
            token_id=str(active_id), user_id=user_id, expires_at=expires_at,
            last_rotation=old_status.get("lastRotationTime"), previous_token_id=previous_id,
            pending_secret_hash=pending_hash, synced=True, conditions=conditions,
        ),
        [existing_secret] if existing_secret else [],
    )


def sync(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    status, _ = sync_with_children(parent, resolver, None)
    return status


def finalize(parent: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    status = parent.get("status") or {}
    user_id = status.get("userId")
    if not user_id:
        try:
            user_id = resolver.user_id((parent.get("spec") or {}).get("userRef", ""))
        except (KeyError, NetBirdError):
            return {}
    token_ids = {
        token_id
        for token_id in (status.get("activeTokenId") or status.get("id"), status.get("previousTokenId"))
        if token_id
    }
    for token_id in token_ids:
        try:
            resolver.client.delete(f"/users/{user_id}/tokens/{token_id}")
        except NotFound:
            pass
    return {}
