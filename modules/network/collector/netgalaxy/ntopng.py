# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Read-only client for ntopng's REST API (the traffic source; ntopng runs on the router).

Only GET requests, with a user that has no admin rights. Listing flows is slow on a small router
(seconds per thousand), so callers ask for the busiest N, not for everything.
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class NtopngError(RuntimeError):
    pass


class Ntopng:
    def __init__(self, url: str, user: str, password: str, *, ifid: int = 0, verify: bool = False, timeout: float = 25.0):
        self.url, self.ifid = url.rstrip("/"), ifid
        self.http = httpx.Client(auth=(user, password), verify=verify, timeout=timeout, follow_redirects=False,
                                 headers={"Accept": "application/json"})
        self.last_error: str | None = None
        self.fetch_seconds = 0.0

    def _get(self, path: str, **params) -> object:
        params = {"ifid": self.ifid, **{k: v for k, v in params.items() if v is not None}}
        try:
            r = self.http.get(self.url + path, params=params)
        except (httpx.ConnectTimeout, httpx.ConnectError) as err:
            # the router answers but nothing listens on ntopng's port: ntopng itself has stopped (it can crash)
            raise NtopngError("ntopng on the router is not answering, so it has probably stopped. "
                              "Start it again in pfSense: Status > Services > ntopng"
                              f" ({type(err).__name__})") from err
        except httpx.HTTPError as err:
            raise NtopngError(f"ntopng unreachable: {type(err).__name__}: {err}") from err
        if r.status_code in (301, 302, 401, 403):
            raise NtopngError("ntopng refused the login (check the user and password)")
        if r.status_code != 200:
            raise NtopngError(f"ntopng answered {r.status_code} for {path}")
        try:
            body = r.json()
        except ValueError as err:
            raise NtopngError(f"ntopng sent something that is not JSON for {path}") from err
        if isinstance(body, dict) and "rsp" in body:
            if body.get("rc", 0) not in (0, None):
                raise NtopngError(f"ntopng error for {path}: {body.get('rc_str_hr') or body.get('rc_str')}")
            return body["rsp"]
        return body

    def _paged(self, path: str, limit: int, per_page: int = 500, **params) -> list[dict]:
        rows: list[dict] = []
        page = 1
        while len(rows) < limit:
            rsp = self._get(path, perPage=min(per_page, limit - len(rows)), currentPage=page, **params)
            data = (rsp or {}).get("data") if isinstance(rsp, dict) else None
            if not data:
                break
            rows += data
            if len(data) < per_page:
                break
            page += 1
        return rows[:limit]

    # ------------------------------------------------------------------ what the collector reads
    def interface(self) -> dict:
        return self._get("/lua/rest/v2/get/interface/data.lua") or {}

    def hosts(self, limit: int = 2000) -> list[dict]:
        """The local hosts ntopng currently tracks, with byte counters. Remote hosts are not listed:
        the flows name the ones that matter, and listing hundreds of them each time is slow."""
        return self._paged("/lua/rest/v2/get/host/active.lua", limit, mode="local")

    def flows(self, limit: int = 600) -> list[dict]:
        """The busiest active flows."""
        return self._paged("/lua/rest/v2/get/flow/active.lua", limit, sortColumn="bytes", sortOrder="desc")

    def host(self, ip: str) -> dict:
        """One host in detail: MAC address, device type, ntopng's behaviour score."""
        return self._get("/lua/rest/v2/get/host/data.lua", host=ip) or {}

    def macs(self, limit: int = 1000) -> dict[str, dict]:
        """MAC -> {vendor, type} for every layer-2 device ntopng has seen."""
        out: dict[str, dict] = {}
        for row in self._paged("/lua/get_macs_data.lua", limit):
            mac = (row.get("key") or "").replace("____", ":").upper()
            if len(mac) != 17:
                continue
            vendor = (row.get("column_manufacturer") or "").strip()
            model = ""
            if "[" in vendor:                      # "Apple, Inc. [ Mac16,1 ]" / "n/a [ J171aAP ]"
                vendor, _, model = vendor.partition("[")
                vendor, model = vendor.strip(), model.strip(" ]")
            out[mac] = {"vendor": "" if vendor.lower() in ("n/a", "") else vendor, "model": model,
                        "type": (row.get("column_device_type") or "").strip()}
        return out

    def snapshot(self, flow_limit: int) -> dict:
        """One refresh worth of data. Raises NtopngError when ntopng cannot be read at all."""
        started = time.monotonic()
        try:
            snap = {"interface": self.interface(), "hosts": self.hosts(), "flows": self.flows(flow_limit)}
        except NtopngError as err:
            self.last_error = str(err)
            raise
        self.last_error = None
        self.fetch_seconds = round(time.monotonic() - started, 2)
        return snap
