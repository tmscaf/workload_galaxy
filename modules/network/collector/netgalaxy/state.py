# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Background refresh loop: read the router, update the inventory, build the graph, cache it for the API."""
from __future__ import annotations

import logging
import threading
import time

from .config import Settings
from .blocks import BlockList
from .devices import Inventory
from .graph import Builder
from .ntopng import Ntopng, NtopngError
from .pinger import Pinger
from .probes import Reachability
from .syslog import EventStore, SyslogReceiver
from .unifi import Unifi, UnifiError
from .vpn import VpnTracker
from .wifi import WifiTracker

log = logging.getLogger(__name__)
MACS_EVERY = 600.0          # the manufacturer table changes slowly
DETAIL_TTL = 3600.0         # re-read a device's detail (MAC, type) this often


class NetState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.started = time.time()
        self.graph: dict | None = None
        self.error: str | None = None
        self.updated_at = 0.0
        self.build_seconds = 0.0
        self.inventory = Inventory(settings.data_dir, settings.site.get("devices"))
        self.events = EventStore(settings.event_window_seconds)
        self.vpn = VpnTracker(settings.data_dir, home_cidrs=[c for c in settings.lan_cidrs if c not in settings.vpn_cidrs],
                              public_address=settings.site.get("publicAddress", ""))
        self.events.vpn = self.vpn
        self.receiver = SyslogReceiver(self.events, settings.syslog_port, settings.syslog_allow)
        self.reach = Reachability()
        protect = list(settings.lan_cidrs) + list(settings.block_protect) + [x for x in (settings.router_ip,
                   settings.site.get("publicAddress", "")) if x] + list(settings.sinkhole_ips)
        self.blocks = BlockList(settings.data_dir, protect, allow_non_global=settings.mode == "sample")
        self._targets = self.reach.targets_from(settings.site) if settings.mode != "sample" else {}
        self.sample = None
        self.ntopng: Ntopng | None = None
        if settings.mode == "sample":
            from .sample import SampleNetwork
            self.sample = SampleNetwork()
        elif settings.ntopng_url:
            self.ntopng = Ntopng(settings.ntopng_url, settings.ntopng_user, settings.ntopng_password,
                                 ifid=settings.ntopng_ifid, verify=settings.ntopng_verify_tls)
        # Wi-Fi and connection health: pings all the time, the UniFi controller every half minute.
        self.pinger = Pinger(settings.data_dir, fast_seconds=settings.ping_fast_seconds, slow_seconds=settings.ping_slow_seconds,
                             dns_server=settings.router_ip if settings.dns_probe_name else "", dns_name=settings.dns_probe_name)
        self.unifi: Unifi | None = None
        if settings.mode != "sample" and settings.unifi_url:
            self.unifi = Unifi(settings.unifi_url, settings.unifi_user, settings.unifi_password, site=settings.unifi_site,
                               verify=settings.unifi_verify_tls)
        self.wifi = WifiTracker(self.pinger, settings.data_dir, router_ip=settings.router_ip, router_name=settings.router_name,
                                isp_gateway=settings.isp_gateway, internet=settings.ping_internet,
                                controller_url=settings.unifi_url, configured=bool(self.unifi or self.sample))
        self._snap: dict | None = None
        self._snap_at = 0.0
        self._counters: dict[str, tuple[float, int, int]] = {}      # ip -> (when, bytes sent, bytes received)
        self._rates: dict[str, tuple[float, float]] = {}
        self._details: dict[str, tuple[float, dict]] = {}
        self._macs: dict[str, dict] = {}
        self._macs_at = 0.0
        self._lock = threading.Lock()
        self._index = None
        self._views: dict[tuple, tuple] = {}
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        if self.settings.mode != "sample":
            self.receiver.start()
            if self.settings.ping:
                self.pinger.set_targets(self.wifi.targets())
                self.pinger.start()
            if self.unifi:
                threading.Thread(target=self._wifi_loop, name="unifi", daemon=True).start()
        threading.Thread(target=self._loop, name="refresh", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.receiver.stop()
        self.inventory.save(force=True)
        self.pinger.stop()
        self.wifi.save(force=True)

    def _wifi_loop(self) -> None:
        """Read the UniFi controller on its own clock, so a slow controller never holds up the map."""
        while not self._stop.is_set():
            try:
                self.wifi.ingest(self.unifi.snapshot())
                self.wifi.fetch_seconds = self.unifi.fetch_seconds
            except UnifiError as err:
                self.wifi.failed(str(err))
            except Exception as err:  # noqa: BLE001 - keep trying
                log.exception("reading the UniFi controller failed")
                self.wifi.failed(f"{type(err).__name__}: {err}")
            self.pinger.set_targets(self.wifi.targets())
            self._stop.wait(self.settings.unifi_seconds)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh_once()
            except Exception as err:  # noqa: BLE001 - keep serving the last good graph
                log.exception("refresh failed")
                self.error = f"{type(err).__name__}: {err}"
            self._stop.wait(self.settings.refresh_seconds)

    def refresh_once(self) -> dict:
        with self._refresh_lock:
            return self._refresh()

    # ------------------------------------------------------------------ one refresh
    def _read(self, now: float) -> str | None:
        """Fetch a new snapshot. Returns an error text when the source failed (the old snapshot stays)."""
        s = self.settings
        if self.sample:
            self._snap, self._snap_at = self.sample.snapshot(now), now
            self._macs, leases = self.sample.macs(), self.sample.leases()
            for ip, lease in leases.items():
                self.events.leases[ip] = lease
            for ip, det in self.sample.details().items():
                self._details[ip] = (now, det)
            if not self.events.events:
                for line in self.sample.syslog_lines():
                    self.events.ingest(line, ts=now - 60)
                self.vpn.ingest_status(self.sample.vpn_status(now), sender="sample", now=now)
            self.wifi.ingest(self.sample.unifi(now), now)
            targets = self.wifi.targets()
            first = not self.pinger.targets
            self.pinger.set_targets(targets)
            for tid in list(targets) + ["dns"]:
                for back in (range(3600, 0, -30) if first else ()):       # an hour of history to look at
                    self.pinger.record(tid, now - back, self.sample.ping_ms(tid, now - back))
                self.pinger.record(tid, now, self.sample.ping_ms(tid, now))
            return None
        if not self.ntopng:
            return "no traffic source configured (set NETGALAXY_NTOPNG_URL)"
        try:
            self._snap, self._snap_at = self.ntopng.snapshot(s.flow_limit), now
        except NtopngError as err:
            return str(err)
        try:
            if now - self._macs_at > MACS_EVERY:
                self._macs, self._macs_at = self.ntopng.macs(), now
            # A few device details per refresh, oldest first: MAC address and device type.
            local = [h["ip"] for h in self._snap["hosts"] if s.is_lan(h.get("ip", "")) and not h.get("is_broadcast")
                     and not h.get("is_multicast") and ":" not in h.get("ip", "")]
            due = sorted((ip for ip in local if now - self._details.get(ip, (0, {}))[0] > DETAIL_TTL),
                         key=lambda ip: self._details.get(ip, (0, {}))[0])
            for ip in due[: s.detail_per_refresh]:
                self._details[ip] = (now, self.ntopng.host(ip))
        except NtopngError as err:
            log.warning("device detail lookup failed: %s", err)
        return None

    def _measure(self, now: float) -> None:
        """Upload and download rate per address, from the change in ntopng's byte counters."""
        rates = {}
        for h in (self._snap or {}).get("hosts", []):
            ip = h.get("ip", "")
            b = h.get("bytes") or {}
            sent, recvd = b.get("sent", 0), b.get("recvd", 0)
            prev = self._counters.get(ip)
            if prev and now - prev[0] >= 5 and sent >= prev[1] and recvd >= prev[2]:
                dt = now - prev[0]
                rates[ip] = ((sent - prev[1]) / dt, (recvd - prev[2]) / dt)
            elif ip in self._rates and prev and now - prev[0] < 5:
                rates[ip] = self._rates[ip]
            if not prev or now - prev[0] >= 5 or sent < prev[1] or recvd < prev[2]:
                self._counters[ip] = (now, sent, recvd)
        self._rates = rates
        if len(self._counters) > 20000:
            cutoff = now - 3600
            self._counters = {k: v for k, v in self._counters.items() if v[0] > cutoff}

    def _refresh(self) -> dict:
        started, now, s = time.monotonic(), time.time(), self.settings
        self.reach.refresh(self._targets, now, wait=self.graph is None)     # the first picture waits for the answers
        source_error = self._read(now)
        fresh = source_error is None
        if fresh:
            self._measure(now)
        snap = self._snap or {"interface": {}, "hosts": [], "flows": []}
        vpn = self.vpn.snapshot(now)
        if not self.sample:
            self.pinger.set_targets(self.wifi.targets())
        wifi = self.wifi.snapshot(now, spark=False)
        ev_status = self.events.status(now)
        listening = bool(s.syslog_port) and s.mode != "sample"
        events = self.events.summary(is_lan=s.is_lan, wan_interfaces=s.wan_interfaces,
                                     blocklist_trackers=s.blocklist_trackers, now=now) if ev_status["events"] else {}
        sources = {
            "mode": s.mode,
            "ntopng": {"enabled": bool(self.ntopng or self.sample), "error": source_error,
                       "fetchSeconds": self.ntopng.fetch_seconds if self.ntopng else 0.0,
                       "hosts": len(snap["hosts"]), "flows": len(snap["flows"]),
                       "ageS": round(now - self._snap_at, 1) if self._snap_at else None},
            "syslog": {"enabled": listening or bool(self.sample), "port": s.syslog_port, "error": self.receiver.error,
                       "uptimeS": round(now - self.started), **ev_status},
            "blocking": {"entries": len(self.blocks.entries), "actions": bool(s.admin_password),
                         "routerSync": bool(s.blocklist_token)},
            "vpn": self._vpn_source(vpn, now),
            "wifi": {**wifi["controller"], "aps": wifi["summary"]["aps"], "clients": wifi["summary"]["clients"]},
            "pings": {**wifi["pings"], "enabled": wifi["pings"]["enabled"] or bool(self.sample), "wanted": s.ping and not self.sample},
            "checks": {"targets": len(self._targets), "up": sum(1 for v in self.reach.alive().values() if v)},
            "inventory": {"devices": len(self.inventory.devices), "learning": self.inventory.learning,
                          "persistent": bool(self.inventory.path)},
            # The keys Workload Galaxy's front end expects, so shared code never trips over a missing one.
            "kubernetes": {"fetchSeconds": 0, "warnings": []}, "metrics": True,
            "agent": {"nodes": 0, "known": [], "stale": [], "agents": []},
            "prometheus": {"enabled": False, "error": None, "edges": 0}, "probes": {"enabled": False, "lastRun": None},
            "configInference": False,
        }
        builder = Builder(s, self.inventory, snap, events=events, leases=self.events.lease_map(), macs=self._macs,
                          details={ip: d for ip, (_t, d) in self._details.items()}, rates=self._rates if fresh else {},
                          sources=sources, now=now, alive=self.reach.alive(), blocks=self.blocks.lookup(), vpn=vpn, wifi=wifi)
        graph = builder.build()
        self.wifi.names = {mac: builder.nodes[nid]["name"] for mac, nid in builder.dev_by_mac.items() if nid in builder.nodes}
        self.build_seconds = round(time.monotonic() - started, 2)
        graph["sources"]["buildSeconds"] = self.build_seconds
        self.inventory.save()
        with self._lock:
            self.graph = graph
            self.updated_at = time.time()
            self.error = source_error
            self._index = None
            self._views.clear()
        return graph

    def _vpn_source(self, vpn: dict, now: float) -> dict:
        s = self.settings
        reporting = vpn["report"]["lastS"] is not None
        error = None
        if vpn["report"]["stale"]:
            error = f"the router's VPN status report stopped {vpn['report']['lastS'] // 60} min ago (its sync script runs every minute)"
        return {"enabled": bool(s.blocklist_token) or bool(self.sample), "reporting": reporting, "error": error,
                "connected": vpn["connected"], "lastReportS": vpn["report"]["lastS"], "logLines": vpn["log"]["lines"],
                "failed24h": vpn["failed24h"], "probes24h": vpn["probes24h"]}

    # ------------------------------------------------------------------ what the API reads
    def current(self) -> dict | None:
        with self._lock:
            return self.graph

    def index(self):
        from .scale import Index
        with self._lock:
            if self._index is None and self.graph is not None:
                self._index = Index(self.graph)
            return self._index

    def view(self, key: tuple, build):
        with self._lock:
            hit = self._views.get(key)
        if hit is None:
            hit = build()
            with self._lock:
                if len(self._views) > 24:
                    self._views.pop(next(iter(self._views)))
                self._views[key] = hit
        return hit
