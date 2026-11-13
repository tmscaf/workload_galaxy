"""Optional active TCP connect probes (GALAXY_PROBES=true, in-cluster only).

Measures connect time to each Service VIP and to a few ready endpoints behind
it. Connect time covers the network path, kube-proxy/OVN NAT and the kernel
accept queue - not application processing - so a slow or failing connect
points at saturated listeners or network trouble. Database/broker ports are
skipped by default (see config.DEFAULT_PROBE_SKIP_PORTS).
"""
from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import Settings
from .util import name_of, ns_of, percentile

MAX_ENDPOINTS_PER_PORT = 3


@dataclass
class ProbeResult:
    attempts: int = 0
    failures: int = 0
    samples_ms: list[float] = field(default_factory=list)

    @property
    def p50(self) -> float | None:
        return percentile(self.samples_ms, 0.5)

    @property
    def p95(self) -> float | None:
        return percentile(self.samples_ms, 0.95)


def _connect(ip: str, port: int, timeout: float = 2.0) -> float | None:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    start = time.perf_counter()
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect((ip, port))
        return (time.perf_counter() - start) * 1000
    except OSError:
        return None


class Prober:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        # "svc:ns/name" -> result for the VIP; ("svc:ns/name", "pod ns/name") -> result per endpoint
        self.vip: dict[str, ProbeResult] = {}
        self.endpoint: dict[tuple[str, str], ProbeResult] = {}
        self.last_run: float = 0.0

    def due(self) -> bool:
        return time.time() - self.last_run >= self.settings.probe_interval_seconds

    def run(self, snap: dict) -> None:
        self.last_run = time.time()
        targets: list[tuple[object, str, int]] = []
        skip = self.settings.probe_skip_ports
        for svc in snap.get("services", []):
            spec = svc.get("spec") or {}
            ip = spec.get("clusterIP")
            if not ip or ip == "None" or spec.get("type") == "ExternalName":
                continue
            sid = f"svc:{ns_of(svc)}/{name_of(svc)}"
            for port in spec.get("ports") or []:
                if (port.get("protocol") or "TCP") == "TCP" and port.get("port") not in skip:
                    targets.append((sid, ip, int(port["port"])))
        for es in snap.get("endpointslices", []):
            svc_name = ((es.get("metadata") or {}).get("labels") or {}).get("kubernetes.io/service-name")
            if not svc_name:
                continue
            sid = f"svc:{ns_of(es)}/{svc_name}"
            ports = [p for p in es.get("ports") or [] if (p.get("protocol") or "TCP") == "TCP"
                     and p.get("port") and p["port"] not in skip]
            ready = [e for e in es.get("endpoints") or [] if (e.get("conditions") or {}).get("ready", True)
                     and (e.get("targetRef") or {}).get("kind") == "Pod"]
            for port in ports[:2]:
                for ep in ready[:MAX_ENDPOINTS_PER_PORT]:
                    ref = ep["targetRef"]
                    for addr in (ep.get("addresses") or [])[:1]:
                        targets.append(((sid, f"{ref.get('namespace', ns_of(es))}/{ref['name']}"), addr, int(port["port"])))

        vip: dict[str, ProbeResult] = {}
        endpoint: dict[tuple[str, str], ProbeResult] = {}
        with ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda t: (t[0], _connect(t[1], t[2])), targets))
        for key, ms in results:
            bucket = vip if isinstance(key, str) else endpoint
            res = bucket.setdefault(key, ProbeResult())
            res.attempts += 1
            if ms is None:
                res.failures += 1
            else:
                res.samples_ms.append(ms)
        with self._lock:
            self.vip, self.endpoint = vip, endpoint

    def results(self) -> tuple[dict[str, ProbeResult], dict[tuple[str, str], ProbeResult]]:
        with self._lock:
            return dict(self.vip), dict(self.endpoint)
