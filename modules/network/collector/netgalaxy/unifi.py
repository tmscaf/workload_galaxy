"""Read-only client for a UniFi Network controller: access points, switches, and who is on Wi-Fi.

It signs in with a controller account (a "View Only" admin is enough) and reads four things:
the adopted devices, the connected clients, the site's health summary, and recent events. It never
writes: the one POST besides the login is the event query, which is how the controller takes a
filter. Works with a self-hosted controller (https://host:8443) and with UniFi OS consoles
(https://host, where the same API lives under /proxy/network).
"""
from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)
LOGIN_RETRY = 300.0         # after a refused login, wait this long before trying again


class UnifiError(RuntimeError):
    pass


class Unifi:
    def __init__(self, url: str, user: str, password: str, *, site: str = "default", verify: bool = False, timeout: float = 15.0,
                 transport=None):
        self.url, self.user, self.password, self.site = url.rstrip("/"), user, password, site or "default"
        self.http = httpx.Client(verify=verify, timeout=timeout, follow_redirects=False, headers={"Accept": "application/json"},
                                 transport=transport)
        self.prefix = ""                # "/proxy/network" on UniFi OS
        self.signed_in = False
        self.refused_at = 0.0
        self.version = ""
        self.fetch_seconds = 0.0
        self.events_work: bool | None = None
        self.events_read = False        # the first read asks for a day of events, later ones for the last hour

    # ------------------------------------------------------------------ session
    def _login(self) -> None:
        if not (self.user and self.password):
            raise UnifiError("no UniFi login is set (NETGALAXY_UNIFI_USER / NETGALAXY_UNIFI_PASSWORD)")
        if self.refused_at and time.time() - self.refused_at < LOGIN_RETRY:
            raise UnifiError("the UniFi controller refused the login (check the user and password); trying again in a few minutes")
        body = {"username": self.user, "password": self.password, "remember": True}
        try:
            r = self.http.post(self.url + "/api/login", json=body)
            if r.status_code == 404:    # UniFi OS console
                r = self.http.post(self.url + "/api/auth/login", json=body)
                self.prefix = "/proxy/network"
            else:
                self.prefix = ""
        except httpx.HTTPError as err:
            raise UnifiError(f"the UniFi controller at {self.url} is not answering ({type(err).__name__})") from err
        if r.status_code in (400, 401, 403):
            self.refused_at = time.time()
            raise UnifiError("the UniFi controller refused the login (check the user and password)")
        if r.status_code == 429:
            self.refused_at = time.time()
            raise UnifiError("the UniFi controller is limiting sign-ins; trying again in a few minutes")
        if r.status_code != 200:
            raise UnifiError(f"the UniFi controller answered {r.status_code} to the login")
        self.refused_at = 0.0
        self.signed_in = True
        token = r.headers.get("X-CSRF-Token") or self.http.cookies.get("csrf_token")
        if token:
            self.http.headers["X-CSRF-Token"] = token

    def _call(self, method: str, path: str, json: dict | None = None, retry: bool = True) -> list:
        if not self.signed_in:
            self._login()
        try:
            r = self.http.request(method, f"{self.url}{self.prefix}/api/s/{self.site}/{path}", json=json)
        except httpx.HTTPError as err:
            raise UnifiError(f"the UniFi controller at {self.url} is not answering ({type(err).__name__})") from err
        if r.status_code in (401, 403) and retry:
            self.signed_in = False          # the session ran out: sign in once more
            return self._call(method, path, json, retry=False)
        if r.status_code != 200:
            raise UnifiError(f"the UniFi controller answered {r.status_code} for {path}")
        try:
            body = r.json()
        except ValueError as err:
            raise UnifiError(f"the UniFi controller sent something that is not JSON for {path}") from err
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, list) else []

    # ------------------------------------------------------------------ what the collector reads
    def snapshot(self, events_within_hours: int = 24) -> dict:
        """One reading. Raises UnifiError when the controller cannot be read at all."""
        started = time.monotonic()
        devices = self._call("GET", "stat/device")
        clients = self._call("GET", "stat/sta")
        health, events = [], []
        try:
            health = self._call("GET", "stat/health")
        except UnifiError as err:
            log.debug("unifi health: %s", err)
        if self.events_work is not False:
            try:
                events = self._call("POST", "stat/event", {"_limit": 500 if self.events_read else 3000, "_sort": "-time",
                                                           "within": 1 if self.events_read else events_within_hours})
                self.events_work = self.events_read = True
            except UnifiError as err:       # newer controllers dropped this endpoint: what is observed is used instead
                if self.events_work is None:
                    log.info("the controller does not serve its event list (%s); using observed events", err)
                    self.events_work = False
        if not self.version:
            try:
                info = self._call("GET", "stat/sysinfo")
                self.version = str((info[0] if info else {}).get("version") or "")
            except UnifiError:
                pass
        self.fetch_seconds = round(time.monotonic() - started, 2)
        return {"devices": devices, "clients": clients, "health": health, "events": events, "version": self.version}
