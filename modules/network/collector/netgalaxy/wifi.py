# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Wi-Fi and connection health: where is the break?

A Wi-Fi device reaches the internet over a chain of hops, and each hop fails in its own way:

    device ~~ radio link ~~ access point -- cable or mesh -- router -- ISP gateway -- the internet

This module joins what the UniFi controller knows about the left half of that chain (which access
point a device is on, how strong its signal is, how often frames are re-sent, how busy the
channel is, when it dropped off or hopped between access points) with what the pinger measures
along the whole chain (round-trip time and loss to the router, the ISP's gateway, the internet,
every access point and every Wi-Fi device). For each device it then names the first hop that looks
wrong, in plain words, with the numbers that say so.

Nothing here changes the controller or the network: it only reads and measures.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

BANDS = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}
PROTO = {"be": "Wi-Fi 7", "ax": "Wi-Fi 6", "ac": "Wi-Fi 5", "n": "Wi-Fi 4", "na": "Wi-Fi 4", "ng": "Wi-Fi 4",
         "g": "802.11g", "b": "802.11b", "a": "802.11a"}
GEAR_STATE = {0: "offline", 1: "connected", 2: "waiting to be adopted", 4: "upgrading", 5: "provisioning", 6: "not reporting",
              7: "adopting", 9: "adoption failed", 10: "managed elsewhere", 11: "isolated"}
GEAR_KIND = {"uap": "Access point", "usw": "Switch", "ugw": "Gateway", "udm": "Gateway", "uxg": "Gateway"}
MAX_EVENTS = 3000
SERIES_KEEP = 1440              # one row per minute, 24 hours
STALE_AFTER = 180.0             # no reading from the controller for this long: say so
DROP_GAP = 600.0                # gone and back within this long = a drop, not someone leaving the house
SAVE_EVERY = 300.0

# What counts as a problem. Signal in dBm (closer to zero is stronger).
SIGNAL_WARN, SIGNAL_BAD = -72, -80
RETRY_WARN, RETRY_BAD = 20.0, 35.0          # % of frames the access point had to send again
RETRY_MIN_FRAMES = 400                      # fewer frames than this since the last figure say nothing (an idle device)
UTIL_WARN, UTIL_BAD = 70, 90                # % of airtime in use on a channel
HOP_LIMITS = {                  # kind -> (warn ms, bad ms, warn loss %, bad loss %)
    "router": (10, 100, 2, 20), "gateway": (40, 150, 2, 20), "internet": (80, 250, 2, 20), "dns": (150, 1000, 5, 30),
    "gear": (20, 150, 2, 20), "sta": (150, 600, 15, 50),
}
RANK = {"unknown": 0, "ok": 1, "warn": 2, "bad": 3, "down": 4}

_EVT = re.compile(r"^EVT_(W[UG]|AP|SW|GW|LU|LG)_(.+)$")
_MAC_TAG = re.compile(r"(User|Guest|AP|SW|GW)\[([0-9a-f:]{17})\]", re.I)


def mac_of(value) -> str:
    return str(value or "").strip().upper()


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _worse(a: str, b: str) -> str:
    return a if RANK.get(a, 0) >= RANK.get(b, 0) else b


def hop_state(kind: str, st: dict | None) -> tuple[str, str]:
    """-> (state, a few words saying why) for one measured hop."""
    if not st or not st.get("sent"):
        return "unknown", "no measurements yet"
    if st["sent"] < (5 if kind == "sta" else 3):
        return "unknown", "still taking the first measurements"
    warn_ms, bad_ms, warn_loss, bad_loss = HOP_LIMITS[kind]
    loss, p50 = st.get("lossPct") or 0.0, st.get("p50Ms")
    if st["sent"] >= 3 and loss >= 100:
        ans = st.get("lastAnswerS")
        return "down", "not answering" + (f" (last answer {_ago(ans)} ago)" if ans is not None else "")
    if loss >= bad_loss:
        return "bad", f"{loss:.0f}% of pings lost"
    if p50 is not None and p50 >= bad_ms:
        return "bad", f"very slow to answer ({p50:.0f} ms)"
    if loss >= warn_loss:
        return "warn", f"{loss:.0f}% of pings lost"
    if p50 is not None and p50 >= warn_ms:
        return "warn", f"slow to answer ({p50:.0f} ms)"
    return "ok", f"{p50:.0f} ms" if p50 is not None and p50 >= 10 else (f"{p50:.1f} ms" if p50 is not None else "answering")


def _ago(seconds) -> str:
    s = int(max(0, seconds or 0))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


class WifiTracker:
    def __init__(self, pinger, data_dir: str = "", *, router_ip: str = "", router_name: str = "Router", isp_gateway: str = "",
                 internet: list[str] | None = None, controller_url: str = "", configured: bool = False):
        self.pinger = pinger
        self.path = os.path.join(data_dir, "wifi.json") if data_dir else ""
        self.router_ip, self.router_name, self.isp_gateway = router_ip, router_name, isp_gateway
        self.internet = list(internet or [])
        self.controller_url, self.configured = controller_url, configured
        self.gear: dict[str, dict] = {}          # access points, switches, gateways by MAC
        self.clients: dict[str, dict] = {}       # everything the controller lists as connected, by MAC
        self.events: deque = deque(maxlen=MAX_EVENTS)
        self.series: dict[str, deque] = {}       # client MAC -> [minute, signal dBm, retry %, experience %]
        self.watch: dict[str, dict] = {}         # MAC -> {ip, name, since}: pinged every few seconds, online or not
        self.names: dict[str, str] = {}          # MAC -> the name the rest of the app shows for that device
        self.read_at = 0.0
        self.error: str | None = None
        self.version = ""
        self.controller_events = False           # the controller's own event list is arriving
        self.fetch_seconds = 0.0
        self._seen_events: deque = deque(maxlen=4000)
        self._seen_set: set = set()
        self._counters: dict[str, tuple] = {}    # MAC -> (retries, attempts) at the last reading
        self._first = True
        self._saved_at = time.time()
        self._lock = threading.RLock()
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.events.extend(data.get("events", [])[-MAX_EVENTS:])
            self.watch = {mac_of(k): v for k, v in (data.get("watch") or {}).items() if isinstance(v, dict)}
            cutoff = time.time() - SERIES_KEEP * 60
            for mac, rows in (data.get("series") or {}).items():
                keep = [r for r in rows if isinstance(r, list) and r and r[0] >= cutoff]
                if keep:
                    self.series[mac] = deque(keep, maxlen=SERIES_KEEP)
            for e in self.events:
                if e.get("id"):
                    self._remember(e["id"])
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as err:
            log.warning("wifi state unreadable (%s); starting empty", err)

    def save(self, force: bool = False) -> None:
        if not self.path or (not force and time.time() - self._saved_at < SAVE_EVERY):
            return
        self._saved_at = time.time()
        with self._lock:
            payload = json.dumps({"events": list(self.events), "watch": self.watch,
                                  "series": {mac: list(rows) for mac, rows in self.series.items()}})
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except OSError as err:
            log.warning("cannot save wifi state: %s", err)

    def _remember(self, eid: str) -> bool:
        """True when this controller event has not been seen before."""
        if eid in self._seen_set:
            return False
        if len(self._seen_events) == self._seen_events.maxlen:
            self._seen_set.discard(self._seen_events[0])
        self._seen_events.append(eid)
        self._seen_set.add(eid)
        return True

    # ------------------------------------------------------------------ one reading of the controller
    def failed(self, error: str) -> None:
        self.error = error

    def ingest(self, raw: dict, now: float | None = None) -> None:
        now = now or time.time()
        with self._lock:
            gear = {}
            for d in raw.get("devices") or []:
                g = self._gear(d)
                if g:
                    gear[g["mac"]] = g
            clients = {}
            for c in raw.get("clients") or []:
                v = self._client(c, gear, now)
                if v:
                    clients[v["mac"]] = v
            self._controller_events(raw.get("events") or [], gear, clients)
            if not self._first:
                self._observe_gear(gear, now)
                if not self.controller_events:
                    self._observe_clients(clients, now)
            minute = int(now // 60) * 60
            for mac, c in clients.items():
                if c["wired"]:
                    continue
                rows = self.series.setdefault(mac, deque(maxlen=SERIES_KEEP))
                row = [minute, c["signal"], c["retryPct"], c["experience"]]
                if rows and rows[-1][0] == minute:
                    rows[-1] = row
                else:
                    rows.append(row)
                if mac in self.watch and c["ip"]:
                    self.watch[mac].update(ip=c["ip"], name=c["name"])
            for mac in [m for m, rows in self.series.items() if m not in clients and (not rows or now - rows[-1][0] > SERIES_KEEP * 60)]:
                self.series.pop(mac)
            self.gear, self.clients = gear, clients
            self.read_at, self.error, self._first = now, None, False
            self.version = str(raw.get("version") or self.version)
        self.save()

    def _gear(self, d: dict) -> dict | None:
        mac = mac_of(d.get("mac"))
        if not mac or d.get("adopted") is False:
            return None
        kind = str(d.get("type") or "")
        up = d.get("uplink") or {}
        cfg = {r.get("name"): r for r in d.get("radio_table") or [] if isinstance(r, dict)}
        radios = []
        for r in d.get("radio_table_stats") or []:
            if not isinstance(r, dict):
                continue
            c = cfg.get(r.get("name")) or {}
            code = r.get("radio") or c.get("radio") or ""
            radios.append({"band": BANDS.get(code, code or "?"), "code": code, "channel": r.get("channel") or c.get("channel"),
                           "width": c.get("ht") or c.get("channel_width"), "util": _num(r.get("cu_total")),
                           "utilOwn": (_num(r.get("cu_self_rx"), 0) or 0) + (_num(r.get("cu_self_tx"), 0) or 0),
                           "clients": int(_num(r.get("num_sta"), 0) or 0), "experience": _pct(r.get("satisfaction")),
                           "txPower": _num(r.get("tx_power")), "retryPct": _ratio(r.get("tx_retries"), r.get("tx_packets"))})
        state = int(_num(d.get("state"), 0) or 0)
        sysstats = d.get("system-stats") or {}
        return {"mac": mac, "kind": kind, "kindLabel": GEAR_KIND.get(kind, "Network device"), "isAp": kind == "uap" or bool(radios),
                "name": str(d.get("name") or f"{d.get('model') or 'UniFi'} ·{mac[-5:]}"), "model": str(d.get("model") or ""),
                "ip": str(d.get("ip") or ""), "state": state, "stateText": GEAR_STATE.get(state, f"state {state}"),
                "online": state == 1, "version": str(d.get("version") or ""), "uptimeS": int(_num(d.get("uptime"), 0) or 0),
                "clients": int(_num(d.get("num_sta"), 0) or 0), "experience": _pct(d.get("satisfaction")),
                "upgradable": bool(d.get("upgradable")), "cpuPct": _num(sysstats.get("cpu")), "memPct": _num(sysstats.get("mem")),
                "lastSeen": _num(d.get("last_seen")),
                "uplink": {"type": "mesh" if up.get("type") == "wireless" else ("wired" if up.get("type") == "wire" else str(up.get("type") or "")),
                           "speed": _num(up.get("speed")), "fullDuplex": up.get("full_duplex"),
                           "to": mac_of(up.get("uplink_mac")), "port": up.get("uplink_remote_port"),
                           "signal": _num(up.get("signal")) if up.get("type") == "wireless" else None},
                "radios": radios}

    def _client(self, c: dict, gear: dict, now: float) -> dict | None:
        mac = mac_of(c.get("mac"))
        if not mac:
            return None
        wired = bool(c.get("is_wired"))
        ap = mac_of(c.get("ap_mac"))
        signal = _num(c.get("signal"))
        if signal is None or signal >= 0:           # some firmware reports only "rssi" (dB above the noise floor)
            rssi = _num(c.get("rssi"))
            signal = rssi - 95 if rssi is not None and rssi > 0 else (rssi if rssi is not None and rssi < 0 else None)
        if signal is not None and not -110 <= signal <= -10:
            signal = None                           # a reading no radio can have: the controller has no real figure yet
        retries, attempts = _num(c.get("tx_retries")), _num(c.get("wifi_tx_attempts")) or _num(c.get("tx_packets"))
        retry_pct = None
        prev = self._counters.get(mac)
        if retries is not None and attempts:
            if prev and attempts - prev[1] >= RETRY_MIN_FRAMES and retries >= prev[0]:
                retry_pct = round(min(100.0, 100.0 * (retries - prev[0]) / (attempts - prev[1])), 1)
            elif prev and prev[2] is not None and attempts >= prev[1]:
                retry_pct = prev[2]             # too few frames since the last reading to say anything new
            elif not prev:
                retry_pct = round(min(100.0, 100.0 * retries / attempts), 1) if attempts >= 200 else None
            if not prev or attempts - prev[1] >= RETRY_MIN_FRAMES or attempts < prev[1]:
                self._counters[mac] = (retries, attempts, retry_pct)
        code = str(c.get("radio") or "")
        name = str(c.get("name") or c.get("hostname") or "").strip()
        return {"mac": mac, "name": name or f"{(c.get('oui') or 'Device').strip() or 'Device'} ·{mac[-5:]}", "named": bool(name),
                "alias": str(c.get("name") or "").strip(), "hostname": str(c.get("hostname") or "").strip(),
                "ip": str(c.get("ip") or ""), "wired": wired, "guest": bool(c.get("is_guest")), "vendor": str(c.get("oui") or ""),
                "ap": ap, "apName": (gear.get(ap) or {}).get("name", ""), "ssid": str(c.get("essid") or ""),
                "band": BANDS.get(code, code), "bandCode": code, "channel": c.get("channel"),
                "proto": PROTO.get(str(c.get("radio_proto") or ""), str(c.get("radio_proto") or "")), "streams": c.get("nss"),
                "signal": None if wired else signal, "noise": _num(c.get("noise")),
                "txMbps": _mbps(c.get("tx_rate")), "rxMbps": _mbps(c.get("rx_rate")),
                "retryPct": None if wired else retry_pct, "experience": _pct(c.get("satisfaction")),
                "uptimeS": int(_num(c.get("uptime"), 0) or 0), "assocAt": _num(c.get("latest_assoc_time")) or _num(c.get("assoc_time")),
                "idleS": int(_num(c.get("idletime"), 0) or 0), "powerSave": bool(c.get("powersave_enabled")),
                "downBps": round((_num(c.get("tx_bytes-r"), 0) or 0) * 8), "upBps": round((_num(c.get("rx_bytes-r"), 0) or 0) * 8),
                "firstSeen": _num(c.get("first_seen")), "seenAt": now}

    # ------------------------------------------------------------------ events
    def _event(self, ts: float, kind: str, **fields) -> None:
        self.events.append({"at": round(ts, 1), "kind": kind, **{k: v for k, v in fields.items() if v not in (None, "")}})

    def _controller_events(self, rows: list, gear: dict, clients: dict) -> None:
        fresh = []
        for e in rows:
            if not isinstance(e, dict) or not e.get("_id") or not self._remember(str(e["_id"])):
                continue
            m = _EVT.match(str(e.get("key") or ""))
            if not m or m.group(1) in ("LU", "LG"):
                continue                    # wired clients plugging in and out are not Wi-Fi events
            who, what = m.group(1), m.group(2)
            ts = (_num(e.get("time"), 0) or 0) / 1000.0 or time.time()
            msg = _MAC_TAG.sub(lambda t: self._label(t.group(2), gear, clients), str(e.get("msg") or ""))
            if who in ("WU", "WG"):
                mac = mac_of(e.get("user") or e.get("guest"))
                name = (clients.get(mac) or self.clients.get(mac) or {}).get("name") or str(e.get("hostname") or "") or mac
                kind = ("roamed" if what.startswith("Roam") else "connected" if what == "Connected"
                        else "disconnected" if what == "Disconnected" else "")
                if not kind:
                    continue
                ap = mac_of(e.get("ap_to") or e.get("ap"))
                src = mac_of(e.get("ap_from"))
                fresh.append({"at": ts, "kind": kind, "id": str(e["_id"]), "mac": mac, "name": name, "ap": ap,
                              "apName": self._gear_name(ap, gear), "from": self._gear_name(src, gear) if src else "",
                              "ssid": str(e.get("ssid") or ""), "duration": _num(e.get("duration")),
                              "band": "changed band" if what == "RoamRadio" else "", "detail": msg, "src": "controller"})
            else:
                mac = mac_of(e.get(who.lower()) or e.get("ap") or e.get("sw") or e.get("gw"))
                kind = ("gear-offline" if "Lost_Contact" in what or what in ("Disconnected", "Isolated")
                        else "gear-online" if what in ("Connected", "Adopted")
                        else "gear-restarted" if "Restarted" in what or "Upgraded" in what
                        else "channel" if "ChannelChange" in what else "interference" if "Interference" in what
                        else "radar" if "Radar" in what else "")
                if not kind:
                    continue
                fresh.append({"at": ts, "kind": kind, "id": str(e["_id"]), "mac": mac, "name": self._gear_name(mac, gear) or str(e.get("ap_name") or mac),
                              "detail": msg, "src": "controller"})
        if fresh:
            self.controller_events = True
            merged = sorted(list(self.events) + [{k: v for k, v in f.items() if v not in (None, "")} for f in fresh], key=lambda e: e["at"])
            self.events = deque(merged[-MAX_EVENTS:], maxlen=MAX_EVENTS)

    def _label(self, mac: str, gear: dict, clients: dict) -> str:
        m = mac_of(mac)
        return (gear.get(m) or clients.get(m) or self.clients.get(m) or {}).get("name") or mac

    def _gear_name(self, mac: str, gear: dict | None = None) -> str:
        return ((gear or self.gear).get(mac) or self.gear.get(mac) or {}).get("name", "") if mac else ""

    def _observe_gear(self, gear: dict, now: float) -> None:
        """Access-point events worked out from the change between two readings."""
        if self.controller_events:
            return
        for mac, g in gear.items():
            old = self.gear.get(mac)
            if not old:
                continue
            if old["online"] and not g["online"]:
                self._event(now, "gear-offline", mac=mac, name=g["name"], detail=f"{g['name']} is {g['stateText']}", src="observed")
            elif not old["online"] and g["online"]:
                self._event(now, "gear-online", mac=mac, name=g["name"], detail=f"{g['name']} is back", src="observed")
            elif g["online"] and old["uptimeS"] > 300 and g["uptimeS"] < old["uptimeS"] - 60:
                self._event(now, "gear-restarted", mac=mac, name=g["name"], detail=f"{g['name']} restarted", src="observed")
            before = {r["code"]: r["channel"] for r in old["radios"]}
            for r in g["radios"]:
                if before.get(r["code"]) and r["channel"] and before[r["code"]] != r["channel"]:
                    self._event(now, "channel", mac=mac, name=g["name"], src="observed",
                                detail=f"{g['name']} moved its {r['band']} radio from channel {before[r['code']]} to {r['channel']}")

    def _observe_clients(self, clients: dict, now: float) -> None:
        """Client events worked out from the change between two readings (used when the controller's
        own event list is not available). A drop shorter than the time between readings still shows,
        because the device's association time restarts."""
        for mac, c in clients.items():
            if c["wired"]:
                continue
            old = self.clients.get(mac)
            common = {"mac": mac, "name": c["name"], "ap": c["ap"], "apName": c["apName"], "ssid": c["ssid"], "src": "observed"}
            if not old or old["wired"]:
                self._event(now, "connected", **common, detail=f"{c['name']} joined {c['apName'] or 'the Wi-Fi'}"
                            + (f" at {c['signal']:.0f} dBm" if c["signal"] is not None else ""))
            elif old["ap"] != c["ap"]:
                self._event(now, "roamed", **common, **{"from": old["apName"]},
                            detail=f"{c['name']} moved from {old['apName'] or 'another access point'} to {c['apName'] or 'another access point'}")
            elif old["bandCode"] != c["bandCode"]:
                self._event(now, "roamed", **common, band="changed band",
                            detail=f"{c['name']} changed from {old['band']} to {c['band']} on {c['apName']}")
            elif (c["assocAt"] and old["assocAt"] and c["assocAt"] > old["assocAt"] + 5) or \
                    (not c["assocAt"] and old["uptimeS"] > 90 and c["uptimeS"] < old["uptimeS"] - 30):
                self._event(now, "reconnected", **common, detail=f"{c['name']} dropped off {c['apName'] or 'the Wi-Fi'} and came straight back")
        for mac, old in self.clients.items():
            if mac not in clients and not old["wired"]:
                self._event(now, "disconnected", mac=mac, name=old["name"], ap=old["ap"], apName=old["apName"], ssid=old["ssid"], src="observed",
                            signal=old["signal"], detail=f"{old['name']} left {old['apName'] or 'the Wi-Fi'}"
                            + (f" (last signal {old['signal']:.0f} dBm)" if old["signal"] is not None else ""))

    def counts(self, mac: str, now: float) -> dict:
        """Drops (gone and back within ten minutes) and hops between access points, last hour and last day."""
        out = {"drops1h": 0, "drops24h": 0, "roams1h": 0, "roams24h": 0, "lastDropS": None}
        gone_at = None
        for e in self.events:
            if e.get("mac") != mac or now - e["at"] > 86400:
                continue
            drop = False
            if e["kind"] == "disconnected":
                gone_at = e["at"]
            elif e["kind"] == "connected":
                drop = gone_at is not None and e["at"] - gone_at <= DROP_GAP
                gone_at = None
            elif e["kind"] == "reconnected":
                drop = True
            elif e["kind"] == "roamed" and not e.get("band"):
                out["roams24h"] += 1
                out["roams1h"] += now - e["at"] <= 3600
            if drop:
                out["drops24h"] += 1
                out["drops1h"] += now - e["at"] <= 3600
                out["lastDropS"] = int(now - e["at"])
        return out

    # ------------------------------------------------------------------ what gets pinged
    def set_watch(self, mac: str, on: bool, ip: str = "", name: str = "") -> dict:
        mac = mac_of(mac)
        with self._lock:
            if on:
                c = self.clients.get(mac) or {}
                self.watch[mac] = {"ip": c.get("ip") or ip, "name": c.get("name") or name or mac, "since": time.time()}
            else:
                self.watch.pop(mac, None)
        self.save(force=True)
        return dict(self.watch)

    def targets(self) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        if self.router_ip:
            out["router"] = (self.router_ip, "fast")
        if self.isp_gateway:
            out["gateway"] = (self.isp_gateway, "fast")
        for ip in self.internet:
            out[f"inet:{ip}"] = (ip, "fast")
        with self._lock:
            for mac, g in self.gear.items():
                if g["ip"]:
                    out[f"gear:{mac}"] = (g["ip"], "fast")
            for mac, c in self.clients.items():
                if c["ip"] and not c["wired"]:
                    out[f"sta:{mac}"] = (c["ip"], "fast" if mac in self.watch else "slow")
            for mac, w in self.watch.items():
                if w.get("ip"):
                    out[f"sta:{mac}"] = (w["ip"], "fast")
        return out

    # ------------------------------------------------------------------ judging
    def _hop(self, tid: str, kind: str, name: str, ip: str, now: float, spark: bool = True) -> dict:
        st = self.pinger.stats(tid, now) if self.pinger else None
        state, why = hop_state(kind, st)
        hour = (self.pinger.stats(tid, now, 3600.0) if self.pinger else None) or {}
        return {"id": tid, "kind": kind, "name": name, "ip": ip, "state": state, "why": why, **(st or {}),
                "hourLossPct": hour.get("lossPct"), "hourP50Ms": hour.get("p50Ms"),
                "spark": self.pinger.history(tid, now, 3600.0, 60) if spark and self.pinger else []}

    def upstream(self, now: float, spark: bool = True) -> dict:
        """The hops every device shares, nearest first, and where (if anywhere) they break."""
        hops = []
        if self.router_ip:
            hops.append(self._hop("router", "router", self.router_name, self.router_ip, now, spark))
        if self.isp_gateway:
            hops.append(self._hop("gateway", "gateway", "ISP gateway", self.isp_gateway, now, spark))
        inet = [self._hop(f"inet:{ip}", "internet", "Internet", ip, now, spark) for ip in self.internet]
        if inet:
            # One public host can drop pings on its own: the internet is as good as the best of them.
            best = min(inet, key=lambda h: (RANK[h["state"]] if h["state"] != "unknown" else 9, h.get("lossPct") or 0, h.get("p50Ms") or 0))
            hops.append({**best, "id": "internet", "name": "Internet", "targets": [{"ip": h["ip"], "state": h["state"], "p50Ms": h.get("p50Ms"),
                                                                                   "lossPct": h.get("lossPct")} for h in inet]})
        if self.pinger and self.pinger.dns_server:
            hops.append(self._hop("dns", "dns", "Name lookups (DNS)", self.pinger.dns_server, now, spark))
        by = {h["id"]: h for h in hops}
        bad = lambda h: h and h["state"] in ("bad", "down")        # noqa: E731
        poor = lambda h: h and h["state"] in ("warn", "bad", "down")  # noqa: E731
        r, g, i, d = by.get("router"), by.get("gateway"), by.get("internet"), by.get("dns")
        level, where, title, detail, fix = "ok", "none", "The path to the internet is healthy", "", ""
        if all(h["state"] == "unknown" for h in hops) or not hops:
            level, title = "unknown", "Still taking the first measurements"
        elif poor(r):
            level, where = ("bad" if bad(r) else "warn"), "router"
            title = f"The router is {'not answering' if r['state'] == 'down' else 'slow or dropping pings'} on the wired network"
            detail = (f"{self.router_name} ({r['ip']}): {r['why']}. Every device goes through it, so everything feels this.")
            fix = ("Check the router's load (Status > Dashboard: CPU, memory, state table) and the cable or switch between it and "
                   "the rest of the network. A router this busy answers pings late even when it still forwards traffic.")
        elif poor(g) and poor(i):
            level, where = ("bad" if bad(i) else "warn"), "isp"
            title = "The internet line itself is struggling"
            detail = (f"Your network is fine up to the router, but the ISP's gateway ({g['ip']}) shows {g['why']} and the internet "
                      f"beyond it shows {i['why']}. That is the line or the ISP's first hop, not your Wi-Fi.")
            fix = "Restart the modem / ONT if it persists, and give the ISP the loss figures and times shown here."
        elif poor(i):
            level, where = ("bad" if bad(i) else "warn"), "internet"
            title = "The internet beyond your ISP's gateway is slow or dropping packets"
            detail = (f"The router{' and the ISP gateway answer' if g else ' answers'} normally, but the internet shows {i['why']}. "
                      "The problem is upstream of your connection.")
            fix = "Usually clears on its own. If it lasts, it is the ISP's network: report it with these figures."
        elif poor(g):
            level, where = "warn", "isp"
            title = "The ISP's gateway is slow to answer, but the internet beyond it is fine"
            detail = f"{g['ip']}: {g['why']}. Gateways often answer pings at low priority; with the internet healthy this is cosmetic."
        elif poor(d):
            level, where = ("bad" if bad(d) else "warn"), "dns"
            title = "Name lookups (DNS) are slow or failing"
            detail = (f"The resolver on {d['ip']} shows {d['why']}. Pages then hang before they start loading even though the "
                      "connection itself is fine, which looks like 'the Wi-Fi is slow'.")
            fix = "On the router check Services > DNS Resolver (and pfBlockerNG's DNSBL if it is on): status, load, and its upstream servers."
        return {"hops": hops, "level": level, "where": where, "title": title, "detail": detail, "fix": fix}

    def _gear_view(self, g: dict, now: float, spark: bool = True) -> dict:
        hop = self._hop(f"gear:{g['mac']}", "gear", g["name"], g["ip"], now, spark) if g["ip"] else {"state": "unknown", "why": ""}
        issues, level = [], "ok"
        if not g["online"]:
            level = "bad"
            issues.append("the controller reports it as isolated: it has power but has lost its link to the network"
                          + (" (it was linked over Wi-Fi to another access point, not by cable)" if g["uplink"]["type"] == "mesh" else "")
                          if g["state"] == 11 else f"the controller reports it as {g['stateText']}")
        elif hop["state"] in ("down", "bad"):
            level = "bad"
            issues.append(f"on the wired side it is {hop['why']}")
        elif hop["state"] == "warn":
            level = "warn"
            issues.append(f"on the wired side it is {hop['why']}")
        up = g["uplink"]
        if g["online"] and g["isAp"]:
            if up["type"] == "mesh":
                level = _worse(level, "warn")
                issues.append("it is linked to the network over Wi-Fi (mesh), which halves its speed and adds delay"
                              + (f"; mesh signal {up['signal']:.0f} dBm" if up.get("signal") else ""))
            elif up["type"] == "wired" and up["speed"] and up["speed"] < 1000:
                level = _worse(level, "warn")
                issues.append(f"its cable has linked at only {up['speed']:.0f} Mbps, usually a damaged cable or a bad port")
            for r in g["radios"]:
                if r["util"] is not None and r["util"] >= UTIL_WARN:
                    level = _worse(level, "bad" if r["util"] >= UTIL_BAD else "warn")
                    theirs = max(0, r["util"] - (r["utilOwn"] or 0))
                    issues.append(f"its {r['band']} channel {r['channel']} is {r['util']:.0f}% busy"
                                  + (f" ({theirs:.0f}% of that is other networks or interference)" if theirs >= 20 else ""))
        return {**g, "level": level, "issues": issues, "ping": {k: hop.get(k) for k in ("state", "why", "p50Ms", "p95Ms", "lossPct", "jitterMs",
                                                                                      "hourLossPct", "lastAnswerS", "sent")},
                "spark": hop.get("spark", [])}

    def _client_view(self, c: dict, now: float, up: dict, gear_views: dict, spark: bool = False) -> dict:
        mac = c["mac"]
        tid = f"sta:{mac}"
        st = self.pinger.stats(tid, now) if self.pinger else None
        hour = (self.pinger.stats(tid, now, 3600.0) if self.pinger else None) or {}
        counts = self.counts(mac, now)
        if not c["named"] and self.names.get(mac):
            c = {**c, "name": self.names[mac]}      # the controller has no name for it; the inventory does
        view = {**c, **counts, "watched": mac in self.watch, "online": True,
                "ping": {**(st or {}), "hourLossPct": hour.get("lossPct"), "hourP50Ms": hour.get("p50Ms")}}
        if spark and self.pinger:
            view["spark"] = self.pinger.history(tid, now, 3600.0, 60)
        view["diag"] = self.diagnose(view, gear_views.get(c["ap"]), up)
        return view

    def diagnose(self, c: dict, ap: dict | None, up: dict) -> dict:
        """Name the first hop that looks wrong for one Wi-Fi device, walking outward from the core."""
        checks = []     # every hop on the device's path with its own verdict, internet first

        def check(hop: str, state: str, text: str) -> None:
            checks.append({"hop": hop, "state": state, "text": text})

        hops = {h["id"]: h for h in up["hops"]}
        for hid, label in (("internet", "Internet"), ("gateway", "ISP gateway"), ("router", "Router")):
            if hid in hops:
                check(label, hops[hid]["state"], hops[hid]["why"])

        level, where, title, detail, fix = "ok", "none", "Connection looks healthy", "", ""
        evidence = []
        name, ap_name = c["name"], c.get("apName") or "its access point"

        def verdict(lv: str, wh: str, t: str, d: str, f: str) -> None:
            nonlocal level, where, title, detail, fix
            if RANK[lv] > RANK[level]:
                level, where, title, detail, fix = lv, wh, t, d, f

        # 1. the access point and its link to the network
        if ap:
            check("Access point", "bad" if not ap["online"] else ap["ping"]["state"] if ap["ping"]["state"] != "unknown" else "ok",
                  ap["stateText"] if not ap["online"] else (ap["ping"]["why"] or "connected"))
            if not ap["online"] or ap["ping"]["state"] in ("down", "bad"):
                verdict("bad", "ap", f"{ap_name} is the problem, not {name}",
                        f"The access point {name} uses is in trouble: {'; '.join(ap['issues'][:2])}. Every device on it is affected.",
                        "Check the access point's power and its network cable (or PoE port). If it restarts on its own, look at "
                        "the PoE budget and the cable first.")
            elif ap["ping"]["state"] == "warn":
                verdict("warn", "ap", f"The link between {ap_name} and the network is unreliable",
                        f"Pings to the access point over the wired side show {ap['ping']['why']}. Wi-Fi devices on it inherit that.",
                        "Check its cable and switch port; a mesh-linked access point needs a better position or a cable.")
            up_link = ap["uplink"]
            if ap["online"] and up_link["type"] == "mesh":
                evidence.append(f"{ap_name} is mesh-linked (no cable), so everything on it gets about half the speed")
                verdict("warn", "ap", f"{ap_name} has no cable: it relays over Wi-Fi",
                        "A mesh-linked access point shares its radio between your devices and its own link back to the network, "
                        "so speed halves and delay and drops rise, most of all when the mesh signal is weak.",
                        "Run a cable to it if at all possible; otherwise move it closer to the access point it links to.")
            elif ap["online"] and up_link["type"] == "wired" and up_link["speed"] and up_link["speed"] < 1000:
                evidence.append(f"{ap_name}'s cable linked at {up_link['speed']:.0f} Mbps")
                verdict("warn", "ap", f"{ap_name}'s cable is running at {up_link['speed']:.0f} Mbps instead of 1000",
                        "The cable or port behind this access point negotiated a slow link. That caps every device on it and "
                        "usually means a damaged cable, a bad connector or a cable with only four working wires.",
                        "Re-seat or replace the cable between the access point and the switch.")
            # 2. the air around it
            radio = next((r for r in ap["radios"] if r["code"] == c.get("bandCode")), None)
            if radio and radio["util"] is not None:
                state = "bad" if radio["util"] >= UTIL_BAD else "warn" if radio["util"] >= UTIL_WARN else "ok"
                check("Airtime", state, f"{radio['band']} channel {radio['channel']} is {radio['util']:.0f}% busy")
                if state != "ok":
                    theirs = max(0, radio["util"] - (radio["utilOwn"] or 0))
                    mostly = "other networks or interference" if theirs > (radio["utilOwn"] or 0) else "your own devices on this access point"
                    verdict(state, "air", f"The {radio['band']} channel {name} is on is crowded ({radio['util']:.0f}% busy)",
                            f"{ap_name} can only talk {100 - radio['util']:.0f}% of the time on channel {radio['channel']}; the rest is taken, "
                            f"mostly by {mostly}. Everything on that radio slows down and stutters.",
                            "Move that radio to a quieter channel (UniFi: Devices > the AP > Radios, or run a channel scan), use 5 GHz "
                            "instead of 2.4 GHz where the device allows, and narrow 2.4 GHz to 20 MHz.")
        elif not c.get("wired"):
            check("Access point", "unknown", "not known")

        # 3. the device's own radio link
        sig, retry = c.get("signal"), c.get("retryPct")
        if sig is not None:
            state = "bad" if sig <= SIGNAL_BAD else "warn" if sig <= SIGNAL_WARN else "ok"
            check("Signal", state, f"{sig:.0f} dBm" + (f" on {c['band']}" if c.get("band") else ""))
            evidence.append(f"signal {sig:.0f} dBm" + (f", link {c['txMbps']:.0f} Mbps down / {c['rxMbps']:.0f} up" if c.get("txMbps") and c.get("rxMbps") else ""))
            if state != "ok":
                sticky = c.get("roams24h", 0) == 0 and c.get("uptimeS", 0) > 1800
                dead = [g["name"] for g in self.gear.values() if g["isAp"] and not g["online"]]
                missing = (f"Note that {' and '.join(dead)} {'is' if len(dead) == 1 else 'are'} offline: devices that normally use "
                           f"{'it' if len(dead) == 1 else 'them'} fall back to a farther access point, which looks exactly like this. "
                           if dead else "")
                verdict(state, "signal", f"{name} has a weak signal to {ap_name} ({sig:.0f} dBm)",
                        f"At {sig:.0f} dBm the link is {'barely usable' if state == 'bad' else 'marginal'}: speeds fall, frames get re-sent, "
                        "and calls and video break up. " + ("It has stayed on this access point without moving to a nearer one, "
                        "which phones and laptops often do until the signal is unusable. " if sticky and not dead else "")
                        + missing
                        + "The rest of the path is fine, so the break is the distance or walls between the device and the access point.",
                        (f"Get {' and '.join(dead)} back on the network first. " if dead else "") +
                        "Move the device or the access point closer, or add an access point near where it is used. If a nearer one "
                        "exists and the device clings to the far one, set a minimum signal (Min RSSI, about -75 dBm) on the far "
                        "access point so it lets go.")
        if retry is not None:
            state = "bad" if retry >= RETRY_BAD else "warn" if retry >= RETRY_WARN else "ok"
            check("Re-sent frames", state, f"{retry:.0f}% of frames had to be sent again")
            evidence.append(f"{retry:.0f}% of frames re-sent")
            if state != "ok" and (sig is None or sig > SIGNAL_WARN):
                verdict(state, "air", f"{name} has a strong signal but a noisy link ({retry:.0f}% of frames re-sent)",
                        f"The signal is fine ({'%.0f dBm' % sig if sig is not None else 'unknown'}), yet {ap_name} has to repeat "
                        f"{retry:.0f}% of what it sends to this device. That is interference: a neighbour's network on the same "
                        "channel, a microwave, Bluetooth, a baby monitor, or a channel set too wide.",
                        "Try another channel on that radio, narrow the channel width (20 MHz on 2.4 GHz, 40 MHz on 5 GHz), and move "
                        "the device off 2.4 GHz if it supports 5 GHz.")
        if c.get("drops1h", 0) >= 3 or c.get("drops24h", 0) >= 6:
            state = "bad" if c.get("drops1h", 0) >= 3 else "warn"
            check("Staying connected", state, f"dropped {c['drops1h']}x in the last hour, {c['drops24h']}x in 24 h")
            evidence.append(f"dropped off Wi-Fi {c['drops24h']} times in 24 h")
            verdict(state, "drops", f"{name} keeps dropping off the Wi-Fi ({c['drops24h']} times in 24 h)",
                    f"It disconnected and came back {c['drops1h']} time(s) in the last hour. With a "
                    + ("weak signal that is expected. " if sig is not None and sig <= SIGNAL_WARN else "healthy signal that points at the device "
                       "itself (power saving, an old Wi-Fi driver) or at a Wi-Fi setting it does not get along with. "),
                    "On the device: update its software or Wi-Fi driver, and forget and re-join the network. On the controller: for "
                    "this network try turning off fast roaming (802.11r) and band steering, and set protected management frames "
                    "(PMF) to optional; older and smart-home devices often drop when those are on.")
        elif c.get("roams1h", 0) >= 6:
            check("Staying connected", "warn", f"hopped between access points {c['roams1h']}x in the last hour")
            evidence.append(f"hopped between access points {c['roams1h']} times in the last hour")
            verdict("warn", "drops", f"{name} keeps hopping between access points ({c['roams1h']} times in the last hour)",
                    "It sits where two access points are about equally strong and flips between them; each hop is a short gap, "
                    "which is what breaks calls and video.",
                    "Lower the transmit power of the access points (Medium or Low instead of Auto/High) so their areas overlap less, "
                    "or set a minimum signal so each lets go at a clear point.")
        else:
            check("Staying connected", "ok", "no drops" if not c.get("drops24h") else f"{c['drops24h']} drop(s) in 24 h")

        # 4. the device itself, end to end
        p = c.get("ping") or {}
        if p.get("sent"):
            pstate, pwhy = hop_state("sta", p)
            asleep = c.get("powerSave") or c.get("idleS", 0) > 60
            if pstate in ("warn", "bad", "down") and asleep:
                check("Answers pings", "ok", f"{pwhy}, but it is idle or asleep, which is normal for a phone or tablet")
            else:
                check("Answers pings", "ok" if pstate == "ok" else pstate, pwhy)
                if pstate in ("bad", "down") and level == "ok":
                    verdict("warn", "device", f"{name} is on the Wi-Fi with a good link, but answers pings poorly",
                            f"Signal, re-sent frames and the access point all look fine, yet {pwhy} while the device was in use. "
                            "That leaves the device: its power saving, a firewall that ignores pings, or its Wi-Fi driver.",
                            "If it is a computer, check its Wi-Fi adapter's power-saving setting and driver. Many phones and smart "
                            "devices ignore pings while idle; watch it while it is actively used before drawing conclusions.")

        # 5. what every device shares
        if up["level"] in ("warn", "bad") and up["where"] != "none":
            if level == "ok":
                level, where, title, detail, fix = up["level"], up["where"], up["title"], \
                    f"{name}'s own Wi-Fi link is fine. " + up["detail"], up["fix"]
            else:
                evidence.append("also: " + up["title"][0].lower() + up["title"][1:])
        if level == "ok" and not c.get("wired"):
            detail = (f"{name} is on {ap_name}" + (f" at {sig:.0f} dBm" if sig is not None else "")
                      + ", the access point and the path to the internet are answering normally, and it has not been dropping.")
        return {"level": level, "where": where, "title": title, "detail": detail, "fix": fix, "evidence": evidence[:6], "checks": checks}

    # ------------------------------------------------------------------ reading
    def status(self, now: float | None = None) -> dict:
        now = now or time.time()
        age = round(now - self.read_at) if self.read_at else None
        error = self.error
        if not error and self.configured and age is not None and age > STALE_AFTER:
            error = f"no reading from the UniFi controller for {_ago(age)}"
        return {"enabled": self.configured, "url": self.controller_url, "version": self.version, "error": error, "ageS": age,
                "events": "controller" if self.controller_events else "observed", "fetchSeconds": self.fetch_seconds}

    def snapshot(self, now: float | None = None, spark: bool = True) -> dict:
        now = now or time.time()
        with self._lock:
            up = self.upstream(now, spark)
            gear = {mac: self._gear_view(g, now, spark) for mac, g in self.gear.items()}
            clients = [self._client_view(c, now, up, gear) for c in self.clients.values() if not c["wired"]]
            online = {c["mac"] for c in clients}
            for mac, w in self.watch.items():      # a watched device that is off the Wi-Fi right now still gets a row
                if mac in online:
                    continue
                st = self.pinger.stats(f"sta:{mac}", now) if self.pinger else None
                counts = self.counts(mac, now)
                clients.append({"mac": mac, "name": w.get("name") or mac, "ip": w.get("ip", ""), "online": False, "watched": True,
                                "wired": False, "ap": "", "apName": "", "signal": None, "retryPct": None, "experience": None,
                                "band": "", "ping": st or {}, **counts,
                                "diag": {"level": "bad" if st and st.get("lossPct", 0) >= 100 else "warn", "where": "offline",
                                         "title": f"{w.get('name') or mac} is not on the Wi-Fi right now",
                                         "detail": "The controller does not list it as connected. If it should be on, it has lost "
                                                   "the Wi-Fi entirely: check that it is powered and in range.",
                                         "fix": "", "evidence": [], "checks": []}})
            order = {"bad": 0, "warn": 1, "ok": 2, "unknown": 3}
            clients.sort(key=lambda c: (order.get(c["diag"]["level"], 3), not c.get("watched"), c.get("signal") if c.get("signal") is not None else 0))
            aps = sorted(gear.values(), key=lambda g: (not g["isAp"], order.get(g["level"], 3), g["name"].lower()))
            tally = {lv: sum(1 for c in clients if c["diag"]["level"] == lv) for lv in ("bad", "warn", "ok")}
            status = self.status(now)
            shown = {c["mac"]: c["name"] for c in clients}
            events = [{**e, "name": shown.get(e.get("mac"), e.get("name", ""))} for e in reversed(self.events) if now - e["at"] <= 86400][:150]
            return {"enabled": self.configured, "controller": status,
                    "pings": {"enabled": bool(self.pinger and self.pinger.started), "error": self.pinger.error if self.pinger else None,
                              "targets": len(self.pinger.targets) if self.pinger else 0},
                    "upstream": up, "gear": aps, "clients": clients, "events": events,
                    "wired": sum(1 for c in self.clients.values() if c["wired"]),
                    "summary": {"aps": sum(1 for g in aps if g["isAp"]), "apsDown": sum(1 for g in aps if g["isAp"] and not g["online"]),
                                "gearProblems": sum(1 for g in aps if g["level"] != "ok"),
                                "clients": sum(1 for c in clients if c["online"]), **tally, "watched": len(self.watch),
                                "drops24h": sum(c.get("drops24h", 0) for c in clients)},
                    "limits": {"signalWarn": SIGNAL_WARN, "signalBad": SIGNAL_BAD, "retryWarn": RETRY_WARN, "retryBad": RETRY_BAD,
                               "utilWarn": UTIL_WARN, "utilBad": UTIL_BAD}}

    def device(self, mac: str, now: float | None = None) -> dict | None:
        """Everything about one Wi-Fi device: its verdict, its path hop by hop, and 24 hours of history."""
        now = now or time.time()
        mac = mac_of(mac)
        with self._lock:
            snap = self.snapshot(now, spark=False)
            view = next((c for c in snap["clients"] if c["mac"] == mac), None)
            if not view:
                return None
            tid = f"sta:{mac}"
            ap = next((g for g in snap["gear"] if g["mac"] == view.get("ap")), None)
            path = [{"id": h["id"], "name": h["name"], "ip": h["ip"], "state": h["state"], "why": h["why"], "p50Ms": h.get("p50Ms"),
                     "lossPct": h.get("lossPct"), "hourLossPct": h.get("hourLossPct")} for h in snap["upstream"]["hops"] if h["id"] != "dns"]
            path.reverse()                      # internet first, the device last
            if ap:
                path.append({"id": f"gear:{ap['mac']}", "name": ap["name"], "ip": ap["ip"], "state": "bad" if not ap["online"] else ap["ping"]["state"],
                             "why": ap["stateText"] if not ap["online"] else ap["ping"]["why"], "p50Ms": ap["ping"].get("p50Ms"),
                             "lossPct": ap["ping"].get("lossPct"), "hourLossPct": ap["ping"].get("hourLossPct"), "uplink": ap["uplink"]})
            p = view.get("ping") or {}
            pstate, pwhy = hop_state("sta", p if p.get("sent") else None)
            path.append({"id": tid, "name": view["name"], "ip": view.get("ip", ""), "state": pstate, "why": pwhy, "p50Ms": p.get("p50Ms"),
                         "lossPct": p.get("lossPct"), "hourLossPct": p.get("hourLossPct"), "signal": view.get("signal"),
                         "band": view.get("band"), "retryPct": view.get("retryPct")})
            hist = {"hour": self.pinger.history(tid, now, 3600.0, 60) if self.pinger else [],
                    "day": self.pinger.history(tid, now, 86400.0, 300) if self.pinger else [],
                    "apDay": self.pinger.history(f"gear:{ap['mac']}", now, 86400.0, 300) if ap and self.pinger else [],
                    "routerDay": self.pinger.history("router", now, 86400.0, 300) if self.pinger else [],
                    "internetDay": self.pinger.history(f"inet:{self.internet[0]}", now, 86400.0, 300) if self.pinger and self.internet else []}
            return {"device": view, "ap": ap, "path": path, "history": hist,
                    "signal": [r for r in self.series.get(mac, []) if now - r[0] <= 86400],
                    "outages": self.pinger.outages(tid, now) if self.pinger else [],
                    "events": [{**e, "name": view["name"]} for e in reversed(self.events) if e.get("mac") == mac and now - e["at"] <= 86400][:80],
                    "upstream": {k: snap["upstream"][k] for k in ("level", "where", "title", "detail")}}

    def by_mac(self, now: float | None = None) -> dict[str, dict]:
        """Wi-Fi facts per device, for the map: MAC -> {ap, signal, band, level, title}."""
        snap = self.snapshot(now, spark=False)
        return {c["mac"]: {"ap": c.get("apName", ""), "signal": c.get("signal"), "band": c.get("band", ""), "ssid": c.get("ssid", ""),
                           "proto": c.get("proto", ""), "retryPct": c.get("retryPct"), "level": c["diag"]["level"], "title": c["diag"]["title"],
                           "where": c["diag"]["where"], "ip": c.get("ip", ""), "name": c.get("name", ""), "named": c.get("named", False),
                           "alias": c.get("alias", ""), "drops24h": c.get("drops24h", 0)} for c in snap["clients"] if c.get("online")}


def _pct(v) -> float | None:
    n = _num(v)
    return None if n is None or n < 0 else n


def _mbps(kbps) -> float | None:
    n = _num(kbps)
    return None if not n else round(n / 1000.0, 1)


def _ratio(part, whole) -> float | None:
    a, b = _num(part), _num(whole)
    return round(min(100.0, 100.0 * a / b), 1) if a is not None and b and b >= 200 else None
