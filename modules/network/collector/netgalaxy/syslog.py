"""Receive the router's remote syslog (UDP) and keep what matters from it.

Three kinds of line are understood:
  filterlog   pfSense's firewall log, one CSV record per packet a logging rule matched
  dhcpd       leases, which tie an address to a MAC address and the name the device gave itself
  openvpn     the VPN server's log (pfSense "VPN Events"), handed to the VPN tracker

Everything else is counted and dropped. Nothing is written to disk: the window is in memory and
starts empty after a restart. Only datagrams from the allowed senders (the router) are read.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from collections import Counter, deque

from .vpn import is_openvpn_line

log = logging.getLogger(__name__)

_FILTERLOG = re.compile(r"filterlog(?:\[\d+\])?[: ].*?(\d+,[^,]*,[^,]*,\d*,[^,]+,[^,]*,(?:pass|block|reject|match|rdr|nat|binat|scrub)\b.*)$")
_DHCPACK = re.compile(r"DHCPACK (?:on|to) (\d+\.\d+\.\d+\.\d+) (?:to )?\(?([0-9a-fA-F:]{17})\)?(?: \(([^)]*)\))?")


def parse_filterlog(line: str) -> dict | None:
    """One firewall log record -> {action, dir, iface, proto, src, dst, sport, dport, tracker}."""
    m = _FILTERLOG.search(line)
    if not m:
        return None
    f = m.group(1).split(",")
    try:
        if len(f) < 9:
            return None
        tracker, iface, action, direction, ipver = f[3], f[4], f[6], f[7], f[8]
        if ipver == "4":
            proto, src, dst, rest = f[16].lower(), f[18], f[19], f[20:]
        elif ipver == "6":
            proto, src, dst, rest = f[12].lower(), f[15], f[16], f[17:]
        else:
            return None
        sport = dport = None
        if proto in ("tcp", "udp") and len(rest) >= 2:
            sport, dport = int(rest[0]), int(rest[1])
    except (IndexError, ValueError):
        return None
    return {"action": action, "dir": direction, "iface": iface, "proto": proto, "src": src, "dst": dst,
            "sport": sport, "dport": dport, "tracker": tracker}


def parse_dhcp(line: str) -> dict | None:
    if "DHCPACK" not in line:
        return None
    m = _DHCPACK.search(line)
    if not m:
        return None
    return {"ip": m.group(1), "mac": m.group(2).upper(), "name": (m.group(3) or "").strip()}


class EventStore:
    """A rolling window of firewall events plus the latest DHCP leases."""

    def __init__(self, window_seconds: int = 3600, max_events: int = 60000):
        self.window = window_seconds
        self.events: deque = deque(maxlen=max_events)       # (ts, action, dir, iface, proto, src, dst, dport, tracker)
        self.leases: dict[str, dict] = {}                   # ip -> {mac, name, ts}
        self.received = 0
        self.dropped_sender = 0
        self.last_at = 0.0
        self.first_at = 0.0
        self.vpn = None                                     # VpnTracker, when VPN lines should be read
        self._lock = threading.Lock()

    def ingest(self, line: str, ts: float | None = None) -> None:
        ts = ts or time.time()
        with self._lock:
            self.received += 1
            self.last_at = ts
            self.first_at = self.first_at or ts
        if self.vpn is not None and is_openvpn_line(line):
            self.vpn.ingest_log(line, ts)
            return
        ev = parse_filterlog(line)
        if ev:
            with self._lock:
                self.events.append((ts, ev["action"], ev["dir"], ev["iface"], ev["proto"], ev["src"], ev["dst"],
                                    ev["dport"], ev["tracker"]))
            return
        lease = parse_dhcp(line)
        if lease:
            with self._lock:
                self.leases[lease["ip"]] = {"mac": lease["mac"], "name": lease["name"], "ts": ts}

    def lease_map(self) -> dict[str, dict]:
        with self._lock:
            return dict(self.leases)

    def status(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            return {"received": self.received, "events": len(self.events), "leases": len(self.leases),
                    "lastMessageS": round(max(0.0, now - self.last_at), 1) if self.last_at else None,
                    "listeningS": round(max(0.0, now - self.first_at), 1) if self.first_at else None,
                    "rejectedSenders": self.dropped_sender}

    def summary(self, *, is_lan, wan_interfaces=(), blocklist_trackers=(), now: float | None = None) -> dict:
        """What the firewall turned away in the window, seen from the outside in and the inside out."""
        now = now or time.time()
        since = now - self.window
        with self._lock:
            events = [e for e in self.events if e[0] >= since]
        wan = set(wan_interfaces)
        bad = set(blocklist_trackers)
        inbound = Counter()          # outside source -> blocked packets
        inbound_bad = Counter()      # ... of which from a known-bad address
        ports = Counter()            # destination port/proto the outside is knocking on
        src_ports: dict[str, set] = {}
        outbound = Counter()         # (lan source, destination) the firewall refused on the way out
        outbound_bad = Counter()
        total_in = total_out = 0
        for _ts, action, direction, iface, proto, src, dst, dport, tracker in events:
            if action not in ("block", "reject"):
                continue
            src_lan, dst_lan = is_lan(src), is_lan(dst)
            if not src_lan and (not wan or iface in wan):
                total_in += 1
                inbound[src] += 1
                if tracker in bad:
                    inbound_bad[src] += 1
                if dport:
                    ports[f"{dport}/{proto}"] += 1
                    src_ports.setdefault(src, set()).add(dport)
            elif src_lan and not dst_lan:
                total_out += 1
                outbound[(src, dst, dport, proto)] += 1
                if tracker in bad:
                    outbound_bad[(src, dst, dport, proto)] += 1
        scanners = sorted(((s, len(p)) for s, p in src_ports.items() if len(p) >= 15), key=lambda x: -x[1])[:10]
        return {
            "windowSeconds": self.window, "events": len(events),
            "blockedIn": total_in, "blockedInSources": len(inbound), "blockedInKnownBad": sum(inbound_bad.values()),
            "knownBadSources": len(inbound_bad),
            "topSources": [{"ip": s, "count": c, "knownBad": s in inbound_bad} for s, c in inbound.most_common(12)],
            "topPorts": [{"port": p, "count": c} for p, c in ports.most_common(12)],
            "scanners": [{"ip": s, "ports": n} for s, n in scanners],
            "blockedOut": total_out,
            "blockedOutPairs": [{"src": k[0], "dst": k[1], "port": f"{k[2]}/{k[3]}" if k[2] else k[3], "count": c,
                                 "knownBad": k in outbound_bad} for k, c in outbound.most_common(40)],
        }


class SyslogReceiver:
    """UDP listener feeding an EventStore. Runs in a daemon thread."""

    def __init__(self, store: EventStore, port: int, allow: list[str]):
        self.store, self.port, self.allow = store, port, set(allow)
        self.sock: socket.socket | None = None
        self.error: str | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not self.port:
            return
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            self.sock.bind(("0.0.0.0", self.port))
            self.sock.settimeout(1.0)
        except OSError as err:
            self.error = f"cannot listen on UDP {self.port}: {err}"
            log.error(self.error)
            self.sock = None
            return
        threading.Thread(target=self._run, name="syslog", daemon=True).start()
        log.info("syslog receiver on UDP %s (accepting %s)", self.port, ", ".join(sorted(self.allow)) or "any sender")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self.sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if self.allow and addr[0] not in self.allow:
                self.store.dropped_sender += 1
                continue
            for line in data.decode("utf-8", "replace").splitlines():
                if line:
                    try:
                        self.store.ingest(line)
                    except Exception:  # noqa: BLE001 - one bad line must not stop the receiver
                        log.debug("unparsable syslog line", exc_info=True)
