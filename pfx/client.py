"""
Thin Pricefx REST client.

Design notes, because they matter when this breaks at 2am:

* Pricefx wraps everything in a `response` envelope and returns HTTP 200 even
  when the operation failed (`response.status` < 0). Checking `resp.ok` alone
  will happily tell you a failed price list creation succeeded. `_unwrap()`
  reads the envelope, always.

* Every endpoint path lives in ENDPOINTS and can be overridden from config.
  Operation names have drifted slightly between Pricefx versions/tenants, so
  rather than hard-coding and hoping, `probe.py` verifies each one against your
  tenant and prints what actually answers. Override in config.yaml if needed --
  no code change.

* One login at process start, one clean exit. No refresh loop, no daemon.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Iterator
from urllib.parse import quote

import requests

log = logging.getLogger("pfx.client")

# Pricefx object type codes used by this tool.
TYPE_PRICE_LIST = "PL"
TYPE_PRICE_LIST_ITEM = "PLI"
TYPE_PRODUCT = "P"
TYPE_PRODUCT_EXTENSION = "PX"

# Endpoint templates, relative to <base>/<partition>/.
# {tc} = type code, {id} = object id.
ENDPOINTS: dict[str, str] = {
    "login": "user.login",
    "logout": "user.logout",
    "fetch": "fetch/{tc}",
    "fetch_paged": "fetch/{tc}/{start}/{end}",
    "add": "add/{tc}",
    "update": "update/{tc}",
    "remove": "remove/{tc}",
    "fetch_extension": "fetchpricelistitems/{id}",
    "pricelist_calculate": "pricelistmanager.calculate/{id}",
    "pricelist_recalculate": "pricelistmanager.recalculate/{id}",
    "pricelist_add_items": "pricelistmanager.additems/{id}",
    "pricelist_submit": "workflow.submitforapproval/{tc}/{id}",
    "executeformula": "executeformula",
}


class PricefxError(RuntimeError):
    """A Pricefx-level failure: bad envelope status, or an HTTP/transport error."""

    def __init__(self, message: str, *, status: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class PricefxClient:
    """One-shot Pricefx session. Use as a context manager so logout always runs."""

    def __init__(
        self,
        base_url: str,
        partition: str,
        *,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
        auth_mode: str = "token",
        timeout: int = 60,
        page_size: int = 500,
        endpoints: dict[str, str] | None = None,
        verify_tls: bool = True,
        dry_run: bool = False,
    ):
        self.base_url = base_url.rstrip("/")
        self.partition = partition
        self.username = username
        self.password = password
        self.token = token
        self.auth_mode = auth_mode.lower()
        self.timeout = timeout
        self.page_size = page_size
        self.endpoints = {**ENDPOINTS, **(endpoints or {})}
        self.dry_run = dry_run

        self.session = requests.Session()
        self.session.verify = verify_tls
        self.session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._logged_in = False

    # ------------------------------------------------------------- plumbing
    def url(self, key: str, **fmt) -> str:
        if key not in self.endpoints:
            raise PricefxError(f"no endpoint configured for {key!r}")
        path = self.endpoints[key].format(**{k: quote(str(v), safe="") for k, v in fmt.items()})
        return f"{self.base_url}/pricefx/{quote(self.partition, safe='')}/{path}"

    def _unwrap(self, resp: requests.Response, *, what: str) -> Any:
        """Pull data out of the Pricefx envelope, raising on a carried failure."""
        if resp.status_code in (401, 403):
            raise PricefxError(
                f"{what}: authentication rejected (HTTP {resp.status_code}). "
                "Check the integration user, its partition and its API permissions.",
                status=resp.status_code,
            )
        try:
            body = resp.json()
        except ValueError:
            snippet = resp.text[:400].replace("\n", " ")
            raise PricefxError(
                f"{what}: expected JSON, got {resp.status_code} "
                f"{resp.headers.get('content-type', '?')}: {snippet}",
                status=resp.status_code,
            ) from None

        env = body.get("response", body) if isinstance(body, dict) else body
        if isinstance(env, dict) and "status" in env:
            status = env.get("status")
            # Pricefx: 0 == success, negative == failure. HTTP 200 either way.
            if isinstance(status, int) and status < 0:
                msg = (
                    env.get("data")
                    or env.get("errors")
                    or env.get("message")
                    or json.dumps(env)[:400]
                )
                raise PricefxError(
                    f"{what}: Pricefx returned status {status}: {msg}",
                    status=status,
                    payload=env,
                )
            return env.get("data", env)

        if not resp.ok:
            raise PricefxError(
                f"{what}: HTTP {resp.status_code}: {resp.text[:400]}",
                status=resp.status_code,
            )
        return env

    def post(self, key: str, payload: dict | None = None, *, what: str | None = None, **fmt) -> Any:
        url = self.url(key, **fmt)
        what = what or key
        log.debug("POST %s %s", url, json.dumps(payload)[:500] if payload else "")
        try:
            resp = self.session.post(url, json=payload or {}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise PricefxError(f"{what}: transport error calling {url}: {exc}") from exc
        return self._unwrap(resp, what=f"{what} [{url}]")

    # ----------------------------------------------------------------- auth
    def login(self) -> "PricefxClient":
        """Authenticate once. Token mode by default, basic as a fallback.

        Both are supported by Pricefx; which one your tenant has enabled is a
        config switch, not a code change.
        """
        if self.auth_mode == "basic":
            if not (self.username and self.password):
                raise PricefxError("basic auth needs username and password")
            raw = f"{self.partition}/{self.username}:{self.password}".encode()
            self.session.headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
            log.info("Using basic auth as %s on partition %s", self.username, self.partition)
            self._logged_in = True
            return self

        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
            log.info("Using pre-issued token on partition %s", self.partition)
            self._logged_in = True
            return self

        if not (self.username and self.password):
            raise PricefxError("token auth needs username and password (or a pre-issued token)")

        data = self.post(
            "login",
            {"data": {"loginName": self.username, "password": self.password}},
            what="login",
        )
        record = data[0] if isinstance(data, list) and data else data
        token = None
        if isinstance(record, dict):
            for key in ("token", "sessionToken", "ssoToken", "authToken"):
                if record.get(key):
                    token = record[key]
                    break
        if not token:
            raise PricefxError(
                "login succeeded but no token field was returned. "
                f"Fields present: {sorted(record) if isinstance(record, dict) else type(record).__name__}. "
                "Set auth.mode: basic in config.yaml, or tell me the field name."
            )
        self.token = token
        self.session.headers["Authorization"] = f"Bearer {token}"
        log.info("Logged in to %s partition %s as %s", self.base_url, self.partition, self.username)
        self._logged_in = True
        return self

    def logout(self) -> None:
        if not self._logged_in or self.auth_mode == "basic":
            return
        try:
            self.post("logout", {}, what="logout")
        except PricefxError as exc:
            # A failed logout must never mask the real result of the run.
            log.debug("logout failed (ignored): %s", exc)
        finally:
            self._logged_in = False
            self.session.headers.pop("Authorization", None)

    def __enter__(self) -> "PricefxClient":
        return self.login()

    def __exit__(self, *exc) -> None:
        self.logout()
        self.session.close()

    # ---------------------------------------------------------------- CRUD
    @staticmethod
    def criteria(*conditions: dict, operator: str = "and") -> dict:
        """Build an SmartClient AdvancedCriteria block, which is what Pricefx fetch expects."""
        return {
            "_constructor": "AdvancedCriteria",
            "operator": operator,
            "criteria": list(conditions),
        }

    @staticmethod
    def crit(field_name: str, operator: str, value: Any) -> dict:
        return {"fieldName": field_name, "operator": operator, "value": value}

    def fetch(
        self,
        type_code: str,
        *,
        criteria: dict | None = None,
        sort_by: list[str] | None = None,
        limit: int | None = None,
    ) -> Iterator[dict]:
        """Page through a fetch, yielding rows.

        A page size is not a total: Pricefx caps rows per request, so a single
        call returning `page_size` rows means "there is more", not "that's all".
        We keep going until a short page arrives or `limit` is reached.
        """
        start = 0
        yielded = 0
        while True:
            end = start + self.page_size - 1
            payload: dict[str, Any] = {"data": criteria or {}}
            if sort_by:
                payload["sortBy"] = sort_by
            rows = self.post(
                "fetch_paged", payload, what=f"fetch {type_code}",
                tc=type_code, start=start, end=end,
            )
            if isinstance(rows, dict):
                rows = rows.get("data", [])
            if not rows:
                return
            for row in rows:
                yield row
                yielded += 1
                if limit and yielded >= limit:
                    return
            if len(rows) < self.page_size:
                return
            start += self.page_size

    def fetch_all(self, type_code: str, **kw) -> list[dict]:
        return list(self.fetch(type_code, **kw))

    def add(self, type_code: str, record: dict) -> dict:
        if self.dry_run:
            log.info("[dry-run] add %s: %s", type_code, json.dumps(record)[:300])
            return {**record, "uniqueName": record.get("uniqueName"), "_dryRun": True}
        data = self.post("add", {"data": record}, what=f"add {type_code}", tc=type_code)
        return data[0] if isinstance(data, list) and data else data

    def update(self, type_code: str, record: dict) -> dict:
        if self.dry_run:
            log.info("[dry-run] update %s: %s", type_code, json.dumps(record)[:300])
            return {**record, "_dryRun": True}
        data = self.post("update", {"data": record}, what=f"update {type_code}", tc=type_code)
        return data[0] if isinstance(data, list) and data else data

    def add_many(self, type_code: str, records: list[dict], *, chunk: int = 200) -> list[dict]:
        """Add records in chunks.

        Pricefx accepts a list payload for bulk add on most tenants; if yours
        rejects it we fall back to one-by-one automatically rather than failing
        the whole run.
        """
        out: list[dict] = []
        for i in range(0, len(records), chunk):
            batch = records[i:i + chunk]
            if self.dry_run:
                log.info("[dry-run] add %d x %s", len(batch), type_code)
                out.extend({**r, "_dryRun": True} for r in batch)
                continue
            try:
                data = self.post("add", {"data": batch}, what=f"bulk add {type_code}", tc=type_code)
                out.extend(data if isinstance(data, list) else [data])
            except PricefxError as exc:
                log.warning("bulk add rejected (%s); falling back to per-record adds", exc)
                for record in batch:
                    out.append(self.add(type_code, record))
        return out
