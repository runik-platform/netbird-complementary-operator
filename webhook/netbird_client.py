"""Shared NetBird REST API client.

Responsibilities:
  * PAT auth via `Authorization: Token <PAT>` header.
  * Configurable exponential backoff with jitter on 429 / 5xx / network errors.
  * Per-request name->ID resolver caches (groups, peers, networks, users) that
    callers build once per sync and pass in, so we don't hammer the API.
  * Structured logging at configurable level.
"""

from __future__ import annotations

import logging
import os
import random
import time
from collections.abc import Iterable, Mapping
from typing import Any

import requests

log = logging.getLogger(__name__)


class NetBirdError(Exception):
    def __init__(self, status: int, body: str, method: str, path: str):
        super().__init__(f"{method} {path} -> {status}: {body[:500]}")
        self.status = status
        self.body = body
        self.method = method
        self.path = path


class NotFound(NetBirdError):
    pass


class NetBirdClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        token_file: str | None = None,
        max_retries: int | None = None,
        base_delay: float | None = None,
        max_delay: float | None = None,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ):
        base = base_url or os.environ.get("NB_MGMT_URL", "")
        if not base:
            raise RuntimeError("NB_MGMT_URL is not set")
        if not base.startswith(("http://", "https://")):
            base = f"https://{base}"
        self.base_url = base.rstrip("/")
        # Precedence is explicit constructor token > token file > NB_API_KEY.
        # A configured file is read for every HTTP attempt so projected
        # Kubernetes Secret updates are consumed without restarting the pod.
        self._explicit_token = token
        self.token_file = token_file if token_file is not None else os.environ.get("NB_API_KEY_FILE")
        self._environment_token = os.environ.get("NB_API_KEY", "")
        if self._explicit_token is None and not self.token_file and not self._environment_token:
            raise RuntimeError("no API token configured (set token, NB_API_KEY_FILE, or NB_API_KEY)")
        self.max_retries = int(
            max_retries
            if max_retries is not None
            else os.environ.get("NB_RATE_LIMIT_MAX_RETRIES", "5")
        )
        self.base_delay = float(
            base_delay
            if base_delay is not None
            else os.environ.get("NB_RATE_LIMIT_BASE_DELAY", "0.5")
        )
        self.max_delay = float(
            max_delay
            if max_delay is not None
            else os.environ.get("NB_RATE_LIMIT_MAX_DELAY", "30")
        )
        self.timeout = timeout
        self.session = session or requests.Session()

    def _token(self) -> str:
        if self._explicit_token is not None:
            token = self._explicit_token.strip()
            if not token:
                raise RuntimeError("explicit API token is empty")
            return token
        if self.token_file:
            try:
                with open(self.token_file, encoding="utf-8") as token_stream:
                    token = token_stream.read().strip()
            except OSError as exc:
                raise RuntimeError(f"cannot read NB_API_KEY_FILE {self.token_file!r}: {exc}") from exc
            if not token:
                raise RuntimeError(f"NB_API_KEY_FILE {self.token_file!r} is empty")
            return token
        if self._environment_token:
            return self._environment_token
        raise RuntimeError("no API token configured")

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        if not path.startswith("/api"):
            path = "/api" + path
        return self.base_url + path

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        url = self._url(path)
        attempt = 0
        while True:
            attempt += 1
            headers = {
                "Authorization": f"Token {self._token()}",
                "Accept": "application/json",
            }
            if json is not None:
                headers["Content-Type"] = "application/json"
            try:
                log.debug("netbird call method=%s path=%s attempt=%d", method, path, attempt)
                resp = self.session.request(
                    method, url, headers=headers, json=json, params=params, timeout=self.timeout
                )
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    log.error("netbird transport error method=%s path=%s err=%s", method, path, exc)
                    raise NetBirdError(0, str(exc), method, path) from exc
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 404:
                raise NotFound(404, resp.text, method, path)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt > self.max_retries:
                    raise NetBirdError(resp.status_code, resp.text, method, path)
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        time.sleep(min(float(retry_after), self.max_delay))
                    except ValueError:
                        self._sleep_backoff(attempt)
                else:
                    self._sleep_backoff(attempt)
                continue
            if not resp.ok:
                raise NetBirdError(resp.status_code, resp.text, method, path)

            if not resp.content:
                return None
            ctype = resp.headers.get("Content-Type", "")
            if "application/json" in ctype:
                return resp.json()
            return resp.text

    def _sleep_backoff(self, attempt: int) -> None:
        delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        delay = delay * (0.5 + random.random())  # full jitter in [0.5, 1.5) * delay
        log.info("netbird backoff sleep=%.2fs attempt=%d", delay, attempt)
        time.sleep(delay)

    # --- Typed helpers ---

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, body: Any) -> Any:
        return self._request("POST", path, json=body)

    def put(self, path: str, body: Any) -> Any:
        return self._request("PUT", path, json=body)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)


class Resolver:
    """Per-sync cache for name->ID lookups.

    Keeps one instance of this per sync hook invocation. Groups are looked up
    with the `?name=X` filter (exact match supported server-side); results are
    cached per-name. Peers/users have no such filter so we list once and index.
    """

    def __init__(self, client: NetBirdClient):
        self.client = client
        self._group_by_name: dict[str, str] = {}
        self._peers_index: dict[str, str] | None = None
        self._users_index: dict[str, str] | None = None
        self._dns_zones_index: dict[str, str] | None = None
        self._networks_index: dict[str, str] | None = None
        self._posture_checks_index: dict[str, str] | None = None

    @staticmethod
    def _build_index(
        items: Iterable[Mapping[str, Any]], key: str = "name"
    ) -> dict[str, str]:
        idx: dict[str, str] = {}
        for it in items:
            name = it.get(key)
            oid = it.get("id")
            if name and oid:
                idx[str(name)] = str(oid)
        return idx

    def group_id(self, name: str) -> str:
        if name in self._group_by_name:
            return self._group_by_name[name]
        items = self.client.get("/groups", params={"name": name}) or []
        if not isinstance(items, list) or any(
            not isinstance(item, Mapping) or not item.get("id") or not item.get("name")
            for item in items
        ):
            raise ValueError(f"incomplete group response while resolving {name!r}")
        hit = next((i for i in items if i.get("name") == name), None)
        if hit and hit.get("id"):
            self._group_by_name[name] = str(hit["id"])
            return self._group_by_name[name]
        raise KeyError(f"group not found: {name!r}")

    def group_ids(self, names: Iterable[str]) -> list[str]:
        return [self.group_id(n) for n in names or []]

    def peer_id(self, name: str) -> str:
        if self._peers_index is None:
            self._peers_index = self._build_index(self.client.get("/peers") or [])
        if name not in self._peers_index:
            raise KeyError(f"peer not found: {name!r}")
        return self._peers_index[name]

    def dns_zone_id(self, name: str) -> str:
        if self._dns_zones_index is None:
            self._dns_zones_index = self._build_index(self.client.get("/dns/zones") or [])
        if name not in self._dns_zones_index:
            raise KeyError(f"dns zone not found: {name!r}")
        return self._dns_zones_index[name]

    def network_id(self, name: str) -> str:
        if self._networks_index is None:
            self._networks_index = self._build_index(self.client.get("/networks") or [])
        if name not in self._networks_index:
            raise KeyError(f"network not found: {name!r}")
        return self._networks_index[name]

    def posture_check_id(self, name: str) -> str:
        if self._posture_checks_index is None:
            self._posture_checks_index = self._build_index(
                self.client.get("/posture-checks") or []
            )
        if name not in self._posture_checks_index:
            raise KeyError(f"posture check not found: {name!r}")
        return self._posture_checks_index[name]

    def posture_check_ids(self, names: Iterable[str]) -> list[str]:
        return [self.posture_check_id(name) for name in names or []]

    def user_id(self, name_or_email: str) -> str:
        if self._users_index is None:
            items = self.client.get("/users") or []
            idx: dict[str, str] = {}
            for u in items:
                if u.get("id"):
                    if u.get("name"):
                        idx[u["name"]] = u["id"]
                    if u.get("email"):
                        idx[u["email"]] = u["id"]
            self._users_index = idx
        if name_or_email not in self._users_index:
            raise KeyError(f"user not found: {name_or_email!r}")
        return self._users_index[name_or_email]
