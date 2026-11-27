# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Latency and loss to the places that matter, measured all the time.

One background thread sends ICMP echo requests ("pings") from where the app runs (a wired machine on
the LAN) and times the answers:

    fast targets   every few seconds: the router, the ISP's gateway, the internet, each access point,
                   and any device you chose to watch
    slow targets   every half minute: every other Wi-Fi device (a sleeping phone should stay asleep)

plus one DNS lookup through the router. Every sample lands in a per-target history: the last hour
sample by sample, and 24 hours as one bucket per minute, kept in latency.json across restarts.
That history is what answers "when did it break, and how far out".

The pings use an unprivileged ICMP socket (SOCK_DGRAM), so the app needs no extra rights: Linux
allows it when net.ipv4.ping_group_range covers the process (it does in a Kubernetes pod), and
macOS allows it always. Nothing but echo requests and one DNS question is ever sent.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import select
import socket
import struct
import threading
import time
from collections import deque

log = logging.getLogger(__name__)

RAW_KEEP = 900              # samples kept one by one per target
MINUTES_KEEP = 1440         # one bucket per minute, 24 hours
TIMEOUT = 2.0               # an echo with no answer after this long is lost
SAVE_EVERY = 300.0


def _checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def echo_request(ident: int, seq: int, token: bytes) -> bytes:
    head = struct.pack("!BBHHH", 8, 0, 0, ident & 0xFFFF, seq & 0xFFFF)
    return head[:2] + struct.pack("!H", _checksum(head + token)) + head[4:] + token


def parse_reply(data: bytes) -> tuple[int, bytes] | None:
    """-> (sequence number, payload) of an echo reply, else None. Linux hands over the ICMP message,
    macOS the whole IP packet."""
    if len(data) >= 28 and data[0] >> 4 == 4:
        data = data[(data[0] & 0x0F) * 4:]
    if len(data) < 8 or data[0] != 0:
        return None
    return struct.unpack("!H", data[6:8])[0], data[8:]


def open_icmp() -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP)
    s.setblocking(False)
    return s


def dns_query(name: str, qid: int) -> bytes:
    q = struct.pack("!HHHHHH", qid & 0xFFFF, 0x0100, 1, 0, 0, 0)
    for label in name.strip(".").split("."):
        q += bytes([len(label)]) + label.encode("ascii", "ignore")
    return q + b"\x00" + struct.pack("!HH", 1, 1)


def dns_time(server: str, name: str, timeout: float = TIMEOUT) -> float | None:
    """Milliseconds for the resolver at `server` to answer one A lookup; None when it did not."""
    qid = int(time.time() * 1000) & 0xFFFF
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        started = time.perf_counter()
        s.sendto(dns_query(name, qid), (server, 53))
        while True:
            data, _ = s.recvfrom(1500)
            if len(data) >= 4 and struct.unpack("!H", data[:2])[0] == qid:
                rcode = data[3] & 0x0F
                return (time.perf_counter() - started) * 1000 if rcode in (0, 3) else None
    except OSError:
        return None
    finally:
        s.close()


class Series:
    """One target's history."""

    def __init__(self):
        self.raw: deque = deque(maxlen=RAW_KEEP)            # (when, ms or None)
        self.minutes: deque = deque(maxlen=MINUTES_KEEP)    # [minute, sent, lost, sum ms, min ms, max ms]

    def add(self, ts: float, ms: float | None) -> None:
        self.raw.append((ts, ms))
        minute = int(ts // 60) * 60
        if not self.minutes or self.minutes[-1][0] != minute:
            if self.minutes and self.minutes[-1][0] > minute:
                return                                      # a sample from the past (clock step): raw only
            self.minutes.append([minute, 0, 0, 0.0, None, None])
        b = self.minutes[-1]
        b[1] += 1
        if ms is None:
            b[2] += 1
        else:
            b[3] += ms
            b[4] = ms if b[4] is None else min(b[4], ms)
            b[5] = ms if b[5] is None else max(b[5], ms)

    def stats(self, now: float, window: float = 300.0) -> dict:
        """What the last `window` seconds looked like. Falls back to minute buckets beyond the raw samples."""
        rows = [(t, ms) for t, ms in self.raw if now - t <= window]
        sent = len(rows)
        got = [ms for _t, ms in rows if ms is not None]
        if len(self.raw) == self.raw.maxlen and now - self.raw[0][0] < window:
            # the window is longer than the raw samples reach: count from the minute buckets instead
            buckets = [b for b in self.minutes if now - b[0] <= window]
            sent = sum(b[1] for b in buckets)
            lost = sum(b[2] for b in buckets)
            avg = sum(b[3] for b in buckets) / max(1, sent - lost)
            worst = max((b[5] for b in buckets if b[5] is not None), default=None)
            return {"sent": sent, "lossPct": round(100.0 * lost / sent, 1) if sent else None, "p50Ms": _r(avg) if sent - lost else None,
                    "p95Ms": _r(worst), "jitterMs": None, "lastMs": _r(self.raw[-1][1]) if self.raw else None,
                    "lastAnswerS": self._last_answer(now), "windowS": int(window)}
        got_sorted = sorted(got)
        jitter = sum(abs(a - b) for a, b in zip(got[1:], got)) / (len(got) - 1) if len(got) > 1 else None
        return {"sent": sent, "lossPct": round(100.0 * (sent - len(got)) / sent, 1) if sent else None,
                "p50Ms": _r(got_sorted[len(got_sorted) // 2]) if got else None,
                "p95Ms": _r(got_sorted[min(len(got_sorted) - 1, int(len(got_sorted) * 0.95))]) if got else None,
                "jitterMs": _r(jitter), "lastMs": _r(self.raw[-1][1]) if self.raw else None,
                "lastAnswerS": self._last_answer(now), "windowS": int(window)}

    def _last_answer(self, now: float) -> int | None:
        for t, ms in reversed(self.raw):
            if ms is not None:
                return int(max(0, now - t))
        for b in reversed(self.minutes):
            if b[1] > b[2]:
                return int(max(0, now - b[0]))
        return None

    def history(self, now: float, span: float = 3600.0, step: int = 60) -> list[list]:
        """[[when, average ms or None, worst ms or None, loss %], ...] oldest first, one row per `step` seconds."""
        out: dict[int, list] = {}
        for minute, sent, lost, total, _lo, hi in self.minutes:
            if now - minute > span:
                continue
            slot = int(minute // step) * step
            row = out.setdefault(slot, [slot, 0, 0, 0.0, None])
            row[1] += sent
            row[2] += lost
            row[3] += total
            if hi is not None:
                row[4] = hi if row[4] is None else max(row[4], hi)
        return [[slot, _r(total / (sent - lost)) if sent > lost else None, _r(hi), round(100.0 * lost / sent, 1) if sent else 0.0]
                for slot, sent, lost, total, hi in (out[k] for k in sorted(out))]

    def outages(self, now: float, span: float = 86400.0, min_loss: float = 50.0) -> list[dict]:
        """Stretches of minutes in which most pings went unanswered: [{from, to, minutes}], newest first."""
        runs, cur = [], None
        for minute, sent, lost, *_ in self.minutes:
            if now - minute > span or not sent:
                continue
            bad = 100.0 * lost / sent >= min_loss
            if bad and cur and minute - cur["to"] <= 120:
                cur["to"] = minute
            elif bad:
                cur = {"from": minute, "to": minute}
                runs.append(cur)
            else:
                cur = None
        return [{"from": r["from"], "to": r["to"] + 60, "minutes": (r["to"] - r["from"]) // 60 + 1} for r in reversed(runs)]


def _r(v: float | None) -> float | None:
    return None if v is None else round(v, 1 if v >= 10 else 2)


class Pinger:
    def __init__(self, data_dir: str = "", *, fast_seconds: float = 5.0, slow_seconds: float = 30.0,
                 dns_server: str = "", dns_name: str = "example.com", dns_seconds: float = 30.0):
        self.path = os.path.join(data_dir, "latency.json") if data_dir else ""
        self.fast, self.slow = max(1.0, fast_seconds), max(5.0, slow_seconds)
        self.dns_server, self.dns_name, self.dns_every = dns_server, dns_name, dns_seconds
        self.series: dict[str, Series] = {}
        self.targets: dict[str, tuple[str, str]] = {}       # id -> (ip, "fast" | "slow")
        self.error: str | None = None
        self.started = False
        self.sent = 0
        self._due: dict[str, float] = {}
        self._pending: dict[int, tuple[str, str, float, float]] = {}    # seq -> (id, ip, sent wall, sent perf)
        self._seq = 0
        self._token = os.urandom(8)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._saved_at = time.time()
        self._dns_at = 0.0
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            cutoff = time.time() - MINUTES_KEEP * 60
            for tid, rows in (data.get("minutes") or {}).items():
                s = self.series.setdefault(tid, Series())
                s.minutes.extend(r for r in rows if isinstance(r, list) and len(r) == 6 and r[0] >= cutoff)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as err:
            log.warning("latency history unreadable (%s); starting empty", err)

    def save(self, force: bool = False) -> None:
        if not self.path or (not force and time.time() - self._saved_at < SAVE_EVERY):
            return
        self._saved_at = time.time()
        with self._lock:
            cutoff = time.time() - MINUTES_KEEP * 60
            for tid in [t for t, s in self.series.items() if t not in self.targets and (not s.minutes or s.minutes[-1][0] < cutoff)]:
                self.series.pop(tid)                         # a device that left a day ago
            payload = json.dumps({"minutes": {tid: list(s.minutes) for tid, s in self.series.items() if s.minutes}})
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except OSError as err:
            log.warning("cannot save latency history: %s", err)

    # ------------------------------------------------------------------ targets and samples
    def set_targets(self, targets: dict[str, tuple[str, str]]) -> None:
        with self._lock:
            self.targets = dict(targets)
            for tid in list(self._due):
                if tid not in self.targets:
                    self._due.pop(tid)

    def record(self, tid: str, ts: float, ms: float | None) -> None:
        with self._lock:
            self.series.setdefault(tid, Series()).add(ts, ms)

    def stats(self, tid: str, now: float | None = None, window: float = 300.0) -> dict | None:
        with self._lock:
            s = self.series.get(tid)
            return s.stats(now or time.time(), window) if s and (s.raw or s.minutes) else None

    def history(self, tid: str, now: float | None = None, span: float = 3600.0, step: int = 60) -> list[list]:
        with self._lock:
            s = self.series.get(tid)
            return s.history(now or time.time(), span, step) if s else []

    def outages(self, tid: str, now: float | None = None, span: float = 86400.0) -> list[dict]:
        with self._lock:
            s = self.series.get(tid)
            return s.outages(now or time.time(), span) if s else []

    # ------------------------------------------------------------------ the loop
    def start(self) -> None:
        if self.started:
            return
        try:
            open_icmp().close()
        except OSError as err:
            self.error = (f"this machine does not let the app send pings ({err.strerror or err}). "
                          "On Linux set net.ipv4.ping_group_range to cover the app's group")
            log.warning("pinger disabled: %s", self.error)
            return
        self.started = True
        threading.Thread(target=self._loop, name="pinger", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.save(force=True)

    def _loop(self) -> None:
        sock = None
        while not self._stop.is_set():
            tick = time.monotonic()
            try:
                sock = sock or open_icmp()
                self._send_due(sock)
                self._collect(sock, until=tick + 1.0)
                self._expire()
                self._dns()
                self.save()
                self.error = None
            except OSError as err:
                self.error = f"cannot send pings: {err.strerror or err}"
                try:
                    if sock:
                        sock.close()
                finally:
                    sock = None
                self._stop.wait(5)
            except Exception:  # noqa: BLE001 - measuring must never stop for good
                log.exception("pinger tick failed")
                self._stop.wait(2)
            self._stop.wait(max(0.0, 1.0 - (time.monotonic() - tick)))

    def _send_due(self, sock: socket.socket) -> None:
        now = time.monotonic()
        with self._lock:
            due = [(tid, ip) for tid, (ip, tier) in self.targets.items() if now >= self._due.get(tid, 0.0)]
            for i, (tid, _ip) in enumerate(due):
                every = self.fast if self.targets[tid][1] == "fast" else self.slow
                # spread first sends over the interval so a hundred devices are not pinged in one burst
                first = tid not in self._due
                self._due[tid] = now + (every * ((i % 20) / 20.0) if first and self.targets[tid][1] != "fast" else every)
        for tid, ip in due:
            self._seq = (self._seq + 1) & 0xFFFF
            try:
                sock.sendto(echo_request(os.getpid(), self._seq, self._token), (ip, 0))
            except OSError as err:
                if err.errno not in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EHOSTDOWN, errno.ENETDOWN, errno.EADDRNOTAVAIL):
                    raise
                self.record(tid, time.time(), None)     # "no route", "host down": the ping is lost
                continue
            self.sent += 1
            self._pending[self._seq] = (tid, ip, time.time(), time.perf_counter())

    def _collect(self, sock: socket.socket, until: float) -> None:
        while self._pending:
            left = until - time.monotonic()
            if left <= 0:
                return
            ready, _, _ = select.select([sock], [], [], min(left, 0.25))
            if not ready:
                continue
            try:
                data, addr = sock.recvfrom(1024)
            except BlockingIOError:
                continue
            got = parse_reply(data)
            if not got or not got[1].startswith(self._token):
                continue
            hit = self._pending.get(got[0])
            if hit and hit[1] == addr[0]:
                self._pending.pop(got[0])
                self.record(hit[0], hit[2], (time.perf_counter() - hit[3]) * 1000)

    def _expire(self) -> None:
        now = time.perf_counter()
        for seq in [s for s, p in self._pending.items() if now - p[3] > TIMEOUT]:
            tid, _ip, wall, _perf = self._pending.pop(seq)
            self.record(tid, wall, None)

    def _dns(self) -> None:
        if not self.dns_server or time.monotonic() - self._dns_at < self.dns_every:
            return
        self._dns_at = time.monotonic()

        def run():
            ts = time.time()
            self.record("dns", ts, dns_time(self.dns_server, self.dns_name))
        threading.Thread(target=run, name="dns-probe", daemon=True).start()


def burst(ips: dict[str, str], count: int = 12, interval: float = 0.4, timeout: float = 1.5) -> dict[str, dict]:
    """Ping several addresses side by side, `count` times each, and summarise: the on-demand test.

    ips: label -> address. Returns label -> {ip, sent, received, lossPct, minMs, p50Ms, maxMs, jitterMs, samples}.
    """
    sock = open_icmp()
    token = os.urandom(8)
    pending: dict[int, tuple[str, float]] = {}
    samples: dict[str, list] = {label: [] for label in ips}
    seq = int(time.time() * 1000) & 0x7FFF
    try:
        for _round in range(count):
            started = time.monotonic()
            for label, ip in ips.items():
                seq = (seq + 1) & 0xFFFF
                try:
                    sock.sendto(echo_request(os.getpid(), seq, token), (ip, 0))
                    pending[seq] = (label, time.perf_counter())
                except OSError:
                    samples[label].append(None)
            _burst_collect(sock, token, ips, pending, samples, started + interval)
        _burst_collect(sock, token, ips, pending, samples, time.monotonic() + timeout)
    finally:
        sock.close()
    for label, _t in pending.values():
        samples[label].append(None)
    out = {}
    for label, rows in samples.items():
        got = [r for r in rows if r is not None]
        ordered = sorted(got)
        out[label] = {"ip": ips[label], "sent": len(rows), "received": len(got),
                      "lossPct": round(100.0 * (len(rows) - len(got)) / len(rows), 1) if rows else None,
                      "minMs": _r(ordered[0]) if got else None, "p50Ms": _r(ordered[len(ordered) // 2]) if got else None,
                      "maxMs": _r(ordered[-1]) if got else None,
                      "jitterMs": _r(sum(abs(a - b) for a, b in zip(got[1:], got)) / (len(got) - 1)) if len(got) > 1 else None,
                      "samples": [_r(r) for r in rows]}
    return out


def _burst_collect(sock, token, ips, pending, samples, until) -> None:
    while True:
        left = until - time.monotonic()
        if left <= 0:
            return
        ready, _, _ = select.select([sock], [], [], left)
        if not ready:
            return
        try:
            data, addr = sock.recvfrom(1024)
        except BlockingIOError:
            continue
        got = parse_reply(data)
        if not got or not got[1].startswith(token):
            continue
        hit = pending.get(got[0])
        if hit and ips.get(hit[0]) == addr[0]:
            pending.pop(got[0])
            samples[hit[0]].append((time.perf_counter() - hit[1]) * 1000)
