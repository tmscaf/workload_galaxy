# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Who is connected to the router's VPN, and what the VPN server has been saying.

Two feeds, both started by the router (the app never connects to it):

  status report   the router's once-a-minute sync script posts the VPN server's own list of
                  connected clients to /api/v1/vpn-status: user, real address, tunnel address,
                  data totals, connected since. This is the source of truth for "who is on now".
  VPN log         pfSense's "VPN Events" remote-log category forwards OpenVPN's log lines over the
                  syslog feed. They make connects and disconnects show within seconds and are the
                  only record of failed logins and of scanners knocking on the VPN port.

Sessions and recent events are kept in vpn.json in the data folder, so a restart of the app does
not forget who is connected or what happened.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
import time
from collections import Counter, deque

log = logging.getLogger(__name__)

STALE_AFTER = 180.0             # no status report for this long: the router stopped reporting
GONE_AFTER = 900.0              # a session the log says connected, never confirmed by a report, is dropped
MAX_EVENTS = 300

_SESSION = re.compile(r"openvpn server '([^']*)' user '([^']*)' address '([^']*?):(\d+)' - (connecting|connected|disconnected)")
_AUTH_FAIL = re.compile(r"user '([^']*)' could not authenticate")
_TLS_AUTH_FAIL = re.compile(r"(?:([^\s/]+)/)?(\d+\.\d+\.\d+\.\d+):(\d+) TLS Auth Error: Auth Username/Password verification failed")
_HMAC = re.compile(r"cannot locate HMAC in incoming packet from \[AF_INET\](\d+\.\d+\.\d+\.\d+):(\d+)")
_TLS_FAIL = re.compile(r"(\d+\.\d+\.\d+\.\d+):(\d+) TLS Error: TLS handshake failed")
_PEER_INFO = re.compile(r"(\d+\.\d+\.\d+\.\d+):(\d+) peer info: (IV_PLAT|IV_GUI_VER|IV_VER)=(\S+)")
_POOL = re.compile(r"([^\s/]+)/(\d+\.\d+\.\d+\.\d+):(\d+) MULTI_sva: pool returned IPv4=(\d+\.\d+\.\d+\.\d+)")

PLATFORMS = {"mac": "Mac", "ios": "iPhone / iPad", "android": "Android", "win": "Windows", "linux": "Linux",
             "freebsd": "FreeBSD", "openbsd": "OpenBSD", "netbsd": "NetBSD", "solaris": "Solaris"}


def is_openvpn_line(line: str) -> bool:
    return "openvpn[" in line or " openvpn:" in line


def _app_name(gui: str) -> str:
    """'OCmacOS_3.8.1-5790' -> 'OpenVPN Connect 3.8.1'; 'net.openvpn.connect.ios_3.4' -> likewise."""
    g = gui or ""
    m = re.search(r"(\d+\.\d+(?:\.\d+)?)", g)
    ver = f" {m.group(1)}" if m else ""
    if g.startswith("OC") or "openvpn.connect" in g.lower():
        return "OpenVPN Connect" + ver
    if "tunnelblick" in g.lower():
        return "Tunnelblick" + ver
    if "viscosity" in g.lower():
        return "Viscosity" + ver
    return g.split("_")[0] if g else ""


class VpnTracker:
    def __init__(self, data_dir: str = "", *, home_cidrs: list[str] | None = None, public_address: str = ""):
        self.path = os.path.join(data_dir, "vpn.json") if data_dir else ""
        self.home = []
        for c in home_cidrs or []:
            try:
                self.home.append(ipaddress.ip_network(c, strict=False))
            except ValueError:
                pass
        self.public_address = public_address
        self.sessions: dict[str, dict] = {}      # key -> session
        self.events: deque = deque(maxlen=MAX_EVENTS)
        self.peer: dict[str, dict] = {}          # "ip:port" -> {platform, app, at}  (from the log, before a report)
        self.probes: Counter = Counter()         # source ip -> rejected packets (no valid VPN key)
        self.probe_times: deque = deque(maxlen=5000)
        self.server: dict = {}                   # name, port, protocol from the last report
        self.report_at = 0.0
        self.report_from = ""
        self.log_lines = 0
        self.log_at = 0.0
        self._lock = threading.Lock()
        self._rates: dict[str, tuple[float, int, int]] = {}
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.sessions = {s["key"]: s for s in data.get("sessions", []) if s.get("key")}
            self.events.extend(data.get("events", [])[-MAX_EVENTS:])
            self.server = data.get("server", {})
            self.report_at = data.get("reportAt", 0.0)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as err:
            log.warning("vpn state unreadable (%s); starting empty", err)

    def _save(self) -> None:
        if not self.path:
            return
        payload = json.dumps({"sessions": list(self.sessions.values()), "events": list(self.events),
                              "server": self.server, "reportAt": self.report_at})
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except OSError as err:
            log.warning("cannot save vpn state: %s", err)

    # ------------------------------------------------------------------ helpers
    def where(self, ip: str) -> str:
        """Where a client is connecting from, as far as can be told without asking anyone."""
        if not ip:
            return ""
        if ip == self.public_address:
            return "your own internet address (inside your network)"
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return ""
        if any(a in n for n in self.home):
            return "inside your network"
        if a.is_private:
            return "a private network"
        return "the internet"

    def _event(self, ts: float, kind: str, **fields) -> None:
        self.events.append({"at": ts, "kind": kind, **{k: v for k, v in fields.items() if v not in (None, "")}})

    @staticmethod
    def _key(user: str, real: str) -> str:
        return f"{user or '?'}@{real}"

    # ------------------------------------------------------------------ the status report (source of truth)
    def ingest_status(self, payload: dict, *, sender: str = "", now: float | None = None) -> int:
        """The router's list of connected clients. Returns how many sessions it held."""
        now = now or time.time()
        servers = payload.get("servers") if isinstance(payload, dict) else None
        if not isinstance(servers, list):
            raise ValueError("expected {'servers': [...]}")
        seen: dict[str, dict] = {}
        server_info = {}
        for srv in servers[:8]:
            if not isinstance(srv, dict):
                continue
            server_info = {"name": str(srv.get("name") or "")[:80], "port": str(srv.get("port") or "")[:8],
                           "mode": str(srv.get("mode") or "")[:40]}
            for c in (srv.get("conns") or [])[:200]:
                if not isinstance(c, dict):
                    continue
                remote = str(c.get("remote_host") or "")
                real, _, rport = remote.rpartition(":") if remote.count(":") == 1 else (remote, "", "")
                user = str(c.get("user_name") or c.get("common_name") or "")[:64]
                if not real:
                    continue
                key = self._key(user, remote)
                def num(k):
                    try:
                        return int(c.get(k) or 0)
                    except (TypeError, ValueError):
                        return 0
                seen[key] = {"user": user, "cert": str(c.get("common_name") or "")[:64], "real": real, "realPort": rport,
                             "tunnel": str(c.get("virtual_addr") or "")[:40], "bytesIn": num("bytes_recv"),
                             "bytesOut": num("bytes_sent"), "since": num("connect_time_unix") or None,
                             "cipher": str(c.get("cipher") or "")[:40], "server": server_info["name"]}
        with self._lock:
            for key, fresh in seen.items():
                old = self.sessions.get(key)
                peer = self.peer.get(f"{fresh['real']}:{fresh['realPort']}", {})
                s = {**(old or {}), **fresh, "key": key, "confirmed": True, "lastReport": now}
                s.setdefault("platform", peer.get("platform", ""))
                s.setdefault("app", peer.get("app", ""))
                if peer.get("platform") and not s.get("platform"):
                    s["platform"] = peer["platform"]
                if peer.get("app") and not s.get("app"):
                    s["app"] = peer["app"]
                # Speed from the change in the totals since the last report.
                prev = self._rates.get(key)
                if prev and now - prev[0] >= 20 and s["bytesIn"] >= prev[1] and s["bytesOut"] >= prev[2]:
                    dt = now - prev[0]
                    s["inBps"], s["outBps"] = round((s["bytesIn"] - prev[1]) * 8 / dt), round((s["bytesOut"] - prev[2]) * 8 / dt)
                if not prev or now - prev[0] >= 20:
                    self._rates[key] = (now, s["bytesIn"], s["bytesOut"])
                if not old:     # connected between two reports with no log line about it
                    self._event(now, "connected", user=s["user"], real=s["real"], tunnel=s["tunnel"], platform=s.get("platform"),
                                where=self.where(s["real"]), note="seen in the router's status report")
                self.sessions[key] = s
            for key in [k for k, s in self.sessions.items() if k not in seen and (s.get("confirmed") or now - s.get("at", now) > GONE_AFTER)]:
                s = self.sessions.pop(key)
                self._rates.pop(key, None)
                if not any(e.get("kind") == "disconnected" and e.get("user") == s.get("user") and e.get("real") == s.get("real")
                           and now - e["at"] < 180 for e in self.events):
                    self._event(now, "disconnected", user=s.get("user"), real=s.get("real"), tunnel=s.get("tunnel"),
                                duration=round(now - s["since"]) if s.get("since") else None, note="no longer in the router's status report")
            self.server = server_info or self.server
            self.report_at, self.report_from = now, sender
            self._save()
        return len(seen)

    # ------------------------------------------------------------------ the VPN log (instant events, failed logins)
    def ingest_log(self, line: str, ts: float | None = None) -> bool:
        """One forwarded OpenVPN log line. Returns True when it meant something."""
        ts = ts or time.time()
        with self._lock:
            self.log_lines += 1
            self.log_at = ts
            m = _HMAC.search(line) or _TLS_FAIL.search(line)
            if m:
                self.probes[m.group(1)] += 1
                self.probe_times.append(ts)
                return True
            m = _PEER_INFO.search(line)
            if m:
                p = self.peer.setdefault(f"{m.group(1)}:{m.group(2)}", {})
                p["at"] = ts
                if m.group(3) == "IV_PLAT":
                    p["platform"] = PLATFORMS.get(m.group(4).lower(), m.group(4))
                elif m.group(3) == "IV_GUI_VER":
                    p["app"] = _app_name(m.group(4))
                if len(self.peer) > 500:
                    for k in sorted(self.peer, key=lambda k: self.peer[k].get("at", 0))[:250]:
                        self.peer.pop(k, None)
                return True
            m = _POOL.search(line)
            if m:
                key = self._key(m.group(1), f"{m.group(2)}:{m.group(3)}")
                self.peer.setdefault(f"{m.group(2)}:{m.group(3)}", {})["tunnel"] = m.group(4)
                if key in self.sessions:
                    self.sessions[key]["tunnel"] = self.sessions[key].get("tunnel") or m.group(4)
                return True
            m = _SESSION.search(line)
            if m:
                server, user, real, rport, what = m.groups()
                key = self._key(user, f"{real}:{rport}")
                peer = self.peer.get(f"{real}:{rport}", {})
                if what == "connected":
                    if key not in self.sessions:
                        self.sessions[key] = {"key": key, "user": user, "real": real, "realPort": rport, "tunnel": peer.get("tunnel", ""),
                                              "since": round(ts), "bytesIn": 0, "bytesOut": 0, "confirmed": False, "at": ts,
                                              "platform": peer.get("platform", ""), "app": peer.get("app", ""), "server": server}
                    self._event(ts, "connected", user=user, real=real, tunnel=peer.get("tunnel"), platform=peer.get("platform"),
                                app=peer.get("app"), where=self.where(real))
                    self._save()
                elif what == "disconnected":
                    s = self.sessions.pop(key, None)
                    self._rates.pop(key, None)
                    self._event(ts, "disconnected", user=user, real=real, tunnel=(s or {}).get("tunnel") or peer.get("tunnel"),
                                duration=round(ts - s["since"]) if s and s.get("since") else None,
                                data=((s or {}).get("bytesIn", 0) + (s or {}).get("bytesOut", 0)) or None)
                    self._save()
                return True
            m = _AUTH_FAIL.search(line)
            if m:
                self._event(ts, "failed", user=m.group(1), note="wrong user name or password")
                self._save()
                return True
            m = _TLS_AUTH_FAIL.search(line)
            if m:
                self._event(ts, "failed", user=m.group(1) or "", real=m.group(2), where=self.where(m.group(2)),
                            note="the VPN server refused the login")
                self._save()
                return True
        return False

    # ------------------------------------------------------------------ reading
    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            # A session only the log knows about, that no report confirmed in time, did not really happen.
            for key in [k for k, s in self.sessions.items() if not s.get("confirmed") and now - s.get("at", now) > GONE_AFTER]:
                self.sessions.pop(key)
            sessions = []
            for s in self.sessions.values():
                d = dict(s)
                d["durationS"] = round(now - s["since"]) if s.get("since") else None
                d["where"] = self.where(s.get("real", ""))
                d.setdefault("inBps", 0)
                d.setdefault("outBps", 0)
                sessions.append(d)
            sessions.sort(key=lambda s: s.get("since") or 0)
            recent = [t for t in self.probe_times if now - t < 86400]
            stale = bool(self.report_at) and now - self.report_at > STALE_AFTER
            return {
                "sessions": sessions, "connected": len(sessions),
                "events": list(reversed(self.events))[:100],
                "failed24h": sum(1 for e in self.events if e.get("kind") == "failed" and now - e["at"] < 86400),
                "probes24h": len(recent),
                "probeSources": [{"ip": ip, "count": c} for ip, c in self.probes.most_common(8)],
                "server": self.server,
                "report": {"lastS": round(now - self.report_at) if self.report_at else None, "stale": stale, "from": self.report_from},
                "log": {"lines": self.log_lines, "lastS": round(now - self.log_at) if self.log_at else None},
            }

    def by_tunnel(self) -> dict[str, dict]:
        with self._lock:
            return {s["tunnel"]: dict(s) for s in self.sessions.values() if s.get("tunnel")}
