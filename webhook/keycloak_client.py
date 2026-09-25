"""Minimal Keycloak Admin API client for complete realm-role snapshots."""

from __future__ import annotations

from collections import deque
from typing import Any
from urllib.parse import quote

import requests


class KeycloakError(Exception):
    """A sanitized provider failure that never includes credentials or bodies."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


class KeycloakClient:
    def __init__(
        self,
        base_url: str,
        realm: str,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 30.0,
        page_size: int = 100,
        session: requests.Session | None = None,
    ):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("provider.baseUrl must be an absolute HTTP(S) URL")
        if not realm or not client_id or not client_secret:
            raise ValueError("provider realm, clientId, and client credential are required")
        self.base_url = base_url.rstrip("/")
        self.realm = realm
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.page_size = page_size
        self.session = session or requests.Session()
        self._access_token: str | None = None

    @property
    def issuer(self) -> str:
        return f"{self.base_url}/realms/{self.realm}"

    def _authenticate(self) -> str:
        realm = quote(self.realm, safe="")
        url = f"{self.base_url}/realms/{realm}/protocol/openid-connect/token"
        try:
            response = self.session.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise KeycloakError("ProviderUnavailable", "Keycloak token endpoint is unavailable") from exc
        if not response.ok:
            reason = "ProviderAuthenticationFailed" if response.status_code in (401, 403) else "ProviderUnavailable"
            raise KeycloakError(reason, f"Keycloak token request failed with HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise KeycloakError("IncompleteProviderResponse", "Keycloak token response is not JSON") from exc
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise KeycloakError("IncompleteProviderResponse", "Keycloak token response has no access token")
        self._access_token = token
        return token

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        token = self._access_token or self._authenticate()
        url = self.base_url + path
        try:
            response = self.session.get(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params=params,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise KeycloakError("ProviderUnavailable", "Keycloak Admin API is unavailable") from exc
        if response.status_code in (401, 403):
            raise KeycloakError("ProviderAuthenticationFailed", f"Keycloak Admin API rejected the service account with HTTP {response.status_code}")
        if response.status_code == 404:
            raise KeycloakError("RealmRoleNotFound", "A configured Keycloak realm role does not exist")
        if not response.ok:
            raise KeycloakError("ProviderUnavailable", f"Keycloak Admin API failed with HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise KeycloakError("IncompleteProviderResponse", "Keycloak Admin API returned non-JSON data") from exc

    def realm_role(self, role_name: str) -> dict[str, Any]:
        realm = quote(self.realm, safe="")
        role = quote(role_name, safe="")
        value = self._get(f"/admin/realms/{realm}/roles/{role}")
        if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not isinstance(value.get("name"), str):
            raise KeycloakError("IncompleteProviderResponse", f"Keycloak realm role {role_name!r} is incomplete")
        return value

    def realm_role_users(self, role_name: str) -> list[dict[str, Any]]:
        realm = quote(self.realm, safe="")
        role = quote(role_name, safe="")

        def paged(path: str, resource: str) -> list[dict[str, Any]]:
            values: list[dict[str, Any]] = []
            seen: set[str] = set()
            first = 0
            while True:
                page = self._get(
                    path,
                    params={"briefRepresentation": "true", "first": first, "max": self.page_size},
                )
                if not isinstance(page, list):
                    raise KeycloakError(
                        "IncompleteProviderResponse",
                        f"Keycloak {resource} for realm role {role_name!r} are not a list",
                    )
                for index, value in enumerate(page):
                    if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
                        raise KeycloakError(
                            "IncompleteProviderResponse",
                            f"Keycloak returned an incomplete {resource[:-1]} for realm role {role_name!r} at page offset {first + index}",
                        )
                    if value["id"] in seen:
                        raise KeycloakError(
                            "IncompleteProviderResponse",
                            f"Keycloak repeated a {resource[:-1]} while paging realm role {role_name!r}",
                        )
                    seen.add(value["id"])
                    values.append(value)
                if len(page) < self.page_size:
                    return values
                first += len(page)

        users_by_id: dict[str, dict[str, Any]] = {}
        direct_users = paged(
            f"/admin/realms/{realm}/roles/{role}/users",
            "users",
        )
        for user in direct_users:
            users_by_id[user["id"]] = user

        groups = deque(
            paged(
                f"/admin/realms/{realm}/roles/{role}/groups",
                "groups",
            )
        )
        seen_groups: set[str] = set()
        while groups:
            group = groups.popleft()
            group_id = group["id"]
            if group_id in seen_groups:
                continue
            seen_groups.add(group_id)
            encoded_group_id = quote(group_id, safe="")

            for user in paged(
                f"/admin/realms/{realm}/groups/{encoded_group_id}/members",
                "users",
            ):
                users_by_id.setdefault(user["id"], user)

            for child in paged(
                f"/admin/realms/{realm}/groups/{encoded_group_id}/children",
                "groups",
            ):
                if child["id"] not in seen_groups:
                    groups.append(child)

        return list(users_by_id.values())
