"""Background refresh loop: snapshot -> telemetry -> graph, cached for the API."""
from __future__ import annotations

import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait

from .config import Settings
from .flows import FlowStore
from .probes import Prober
from .prom import PrometheusSource
from .topology import Builder, public

log = logging.getLogger(__name__)
DNS_TTL = 300


class GalaxyState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.flows = FlowStore()
        self.prom = PrometheusSource(settings)
        self.prober = Prober(settings)
        self.graph: dict | None = None
        self.error: str | None = None
        self.updated_at = 0.0
        self.build_seconds = 0.0
        self._lock = threading.Lock()
        self._index = None
        self._views: dict[tuple, tuple] = {}
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._probe_thread: threading.Thread | None = None
        self._dns: dict[str, tuple[float, list[str]]] = {}
        self._dns_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns")
        if settings.mode == "demo":
            import os

            from .demo import DemoSource
            self.source = DemoSource()
            from .scenarios import PRESETS
            preset = [s for s in os.environ.get("GALAXY_DEMO_SCENARIOS", "").split(",") if s.strip()]
            named = os.environ.get("GALAXY_DEMO_PRESET", "").strip()       # a curated combination, by id
            preset += next((p["scenarios"] for p in PRESETS if p["id"] == named), [])
            if preset:
                self.source.set_scenarios([s.strip() for s in preset])
            self.in_cluster = False
        else:
            from .kube import KubeSource
            self.source = KubeSource(settings)
            self.in_cluster = self.source.in_cluster

    # ------------------------------------------------------------------ loop
    def start(self) -> None:
        threading.Thread(target=self._loop, name="refresh", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

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

    def _refresh(self) -> dict:
        started = time.monotonic()
        snap = self.source.snapshot()
        demo = self.settings.mode == "demo"
        reports = self.source.reports() if demo else self.flows.fresh_reports(self.settings.flow_window_seconds)
        mesh = self.source.mesh() if demo else self.prom.fetch()
        if self.settings.probes and self.in_cluster and self.prober.due() and \
                not (self._probe_thread and self._probe_thread.is_alive()):
            self._probe_thread = threading.Thread(target=self.prober.run, args=(snap,), daemon=True)
            self._probe_thread.start()
        hidden = set(self.settings.exclude_namespaces)
        if self.settings.hide_self and self.settings.self_namespace:
            hidden.add(self.settings.self_namespace)
        sources = {
            "mode": self.settings.mode,
            "kubernetes": {"fetchSeconds": snap.get("fetchSeconds"), "warnings": snap.get("warnings", [])},
            "metrics": bool(snap.get("podmetrics")),
            "agent": (self.source.agent_status() if demo else self.flows.status(self.settings.flow_window_seconds)),
            "prometheus": {"enabled": self.prom.enabled or demo, "error": self.prom.last_error,
                           "edges": len(mesh)},
            "probes": {"enabled": self.settings.probes and self.in_cluster,
                       "lastRun": self.prober.last_run or None},
            "configInference": self.settings.read_configmaps,
        }
        builder = Builder(snap, reports=reports, mesh=mesh, probes=self.prober.results(),
                          ext_dns=self.source.dns() if demo else self._ext_dns(),
                          hidden_namespaces=hidden, sources=sources,
                          flow_idle_seconds=self.settings.flow_idle_seconds,
                          internal_cidrs=self.settings.internal_cidrs,
                          event_window_seconds=self.settings.event_window_seconds,
                          group_by=self.settings.group_by, group_separator=self.settings.group_separator)
        graph = public(builder.build())
        if self.settings.resolve_external_hosts and not demo:
            self._resolve_hosts(graph.get("externalHosts", []))
        self.build_seconds = round(time.monotonic() - started, 2)
        graph["sources"]["buildSeconds"] = self.build_seconds
        with self._lock:
            self.graph = graph
            self.updated_at = time.time()
            self.error = None
            self._index = None
            self._views.clear()
        return graph

    def current(self) -> dict | None:
        with self._lock:
            return self.graph

    def index(self):
        """Lookups over the current graph (adjacency, table rows), built once per refresh."""
        from .scale import Index
        with self._lock:
            if self._index is None and self.graph is not None:
                self._index = Index(self.graph)
            return self._index

    def view(self, key: tuple, build):
        """What one combination of filter and zoom level looks like, cached until the next refresh."""
        with self._lock:
            hit = self._views.get(key)
        if hit is None:
            hit = build()
            with self._lock:
                if len(self._views) > 24:
                    self._views.pop(next(iter(self._views)))
                self._views[key] = hit
        return hit

    # ------------------------------------------------------------------ DNS for external hosts
    def _ext_dns(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for host, (_, ips) in list(self._dns.items()):
            for ip in ips:
                out.setdefault(ip, host)
        return out

    def _resolve_hosts(self, hosts: list[str]) -> None:
        now = time.time()
        todo = [h for h in hosts if h not in self._dns or now - self._dns[h][0] > DNS_TTL][:50]
        if not todo:
            return

        def lookup(host: str) -> tuple[str, list[str]]:
            try:
                infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
                return host, sorted({i[4][0] for i in infos})
            except OSError:
                return host, []

        futures = [self._dns_pool.submit(lookup, h) for h in todo]
        done, _ = wait(futures, timeout=3)
        for f in done:
            host, ips = f.result()
            self._dns[host] = (now, ips)
