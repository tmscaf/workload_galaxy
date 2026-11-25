"""Is a server there? A cheap reachability check for the few addresses where a wrong answer matters.

ntopng only knows a device while it is sending or receiving, and forgets it after a few idle
minutes. An idle server would then look "off" and its port forwards "dead". For port-forward
targets and devices named in the site file, the collector therefore opens (and at once closes) one
TCP connection: an answer of any kind, accepted or refused, proves the machine is up. Nothing is
sent, and database ports are never used (a half-open connection counts as an error there).
"""
from __future__ import annotations

import errno
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

SKIP_PORTS = {3306, 5432, 1433, 1521, 27017, 6379, 9042, 9092, 11211, 5672, 9200}
FALLBACK_PORTS = (22, 80, 443)
UP_ERRORS = {errno.ECONNREFUSED, errno.ECONNRESET}


def tcp_alive(ip: str, ports: list[int], timeout: float = 1.0) -> bool:
    for port in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((ip, port))
            return True
        except OSError as err:
            if err.errno in UP_ERRORS:
                return True         # something answered "no": the machine is on
        finally:
            s.close()
    return False


class Reachability:
    def __init__(self, every: float = 120.0, check=tcp_alive):
        self.every, self.check = every, check
        self.results: dict[str, tuple[float, bool]] = {}
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="probe")
        self._lock = threading.Lock()
        self._running = False

    def targets_from(self, site: dict) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for fw in site.get("forwards", []):
            ip = fw.get("target")
            if not ip:
                continue
            ports = out.setdefault(ip, [])
            first = str(fw.get("targetPort") or fw.get("port") or "").replace(":", "-").split("-")[0]
            if (fw.get("proto") or "tcp").lower() == "tcp" and first.isdigit() and int(first) not in SKIP_PORTS:
                ports.append(int(first))
        for d in site.get("devices", []):
            if d.get("ip"):
                out.setdefault(d["ip"], [])
        return {ip: (ports + [p for p in FALLBACK_PORTS if p not in ports])[:4] for ip, ports in out.items()}

    def refresh(self, targets: dict[str, list[int]], now: float | None = None, wait: bool = False) -> None:
        """Start a round in the background when the last one is old enough. Never blocks a refresh."""
        now = now or time.time()
        due = {ip: ports for ip, ports in targets.items() if now - self.results.get(ip, (0, False))[0] >= self.every}
        if not due or self._running:
            return
        self._running = True

        def run():
            try:
                futures = {ip: self._pool.submit(self.check, ip, ports) for ip, ports in due.items()}
                for ip, fut in futures.items():
                    try:
                        ok = bool(fut.result(timeout=15))
                    except Exception:  # noqa: BLE001
                        ok = False
                    with self._lock:
                        self.results[ip] = (time.time(), ok)
            finally:
                self._running = False
        if wait:
            run()
        else:
            threading.Thread(target=run, name="probes", daemon=True).start()

    def alive(self) -> dict[str, bool]:
        """ip -> up/down, for the addresses that have been checked."""
        with self._lock:
            return {ip: ok for ip, (_t, ok) in self.results.items()}
