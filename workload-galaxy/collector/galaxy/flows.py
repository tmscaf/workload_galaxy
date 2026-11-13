"""Storage and aggregation of what the node agents observe.

Each agent POSTs a snapshot every few seconds containing, per pod network
namespace, its TCP sockets (with kernel RTT, retransmits and queue depths) and
listening sockets (accept-queue depth), plus the node's conntrack table (TCP and
UDP flows with original/reply tuples). The store keeps the latest report per
node, turns byte counters into rates, and aggregates everything into
observations between resolved entities.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Hashable

Entity = tuple[str, str]   # ("pod", "ns/name") | ("svc", id) | ("node", name) | ("ext", ip) | ("clients", "")
Resolver = Callable[[str, int | None, str], Entity | None]


@dataclass
class FlowStats:
    connections: int = 0
    connections_in: int = 0
    pending: int = 0              # SYN-SENT sockets / unreplied conntrack entries: connects not answered
    rtts_ms: list[float] = field(default_factory=list)      # kernel smoothed RTT (inflated by delayed ACKs)
    minrtts_ms: list[float] = field(default_factory=list)   # kernel minimum RTT: the network path floor
    retrans: int = 0              # segments currently being retransmitted
    retrans_total: int = 0
    send_q: int = 0               # bytes the client could not get acknowledged yet
    recv_q: int = 0               # bytes received but not yet read by the server app
    bytes_per_sec: float = 0.0
    sources: set[str] = field(default_factory=set)   # "socket" | "conntrack"
    ports: set[str] = field(default_factory=set)
    idle_s: float | None = None   # seconds since the freshest underlying flow saw a packet (None = unknown)

    def merge(self, other: "FlowStats") -> None:
        if other.idle_s is not None:
            self.idle_s = other.idle_s if self.idle_s is None else min(self.idle_s, other.idle_s)
        self.connections += other.connections
        self.connections_in += other.connections_in
        self.pending += other.pending
        self.rtts_ms += other.rtts_ms
        self.minrtts_ms += other.minrtts_ms
        self.retrans += other.retrans
        self.retrans_total += other.retrans_total
        self.send_q += other.send_q
        self.recv_q += other.recv_q
        self.bytes_per_sec += other.bytes_per_sec
        self.sources |= other.sources
        self.ports |= other.ports


@dataclass
class ListenStats:
    queue: int = 0
    backlog: int = 0


@dataclass
class Observations:
    flows: dict[tuple[Entity, Entity], FlowStats] = field(default_factory=dict)
    listen: dict[str, ListenStats] = field(default_factory=dict)    # pod uid -> accept queue
    nodes_reporting: list[str] = field(default_factory=list)
    capabilities: dict[str, dict] = field(default_factory=dict)

    def add(self, src: Entity, dst: Entity, stats: FlowStats) -> None:
        if src == dst:
            return
        key = (src, dst)
        if key in self.flows:
            self.flows[key].merge(stats)
        else:
            self.flows[key] = stats


class FlowStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reports: dict[str, dict] = {}
        self._counters: dict[str, dict[Hashable, tuple[float, float]]] = {}

    def ingest(self, report: dict) -> None:
        node = str(report.get("node") or "unknown")
        now = time.time()
        report["receivedAt"] = now
        with self._lock:
            prev = self._counters.get(node, {})
            current: dict[Hashable, tuple[float, float]] = {}
            for netns in report.get("netns") or []:
                uid = netns.get("podUid", "")
                for s in netns.get("sockets") or []:
                    key = ("s", uid, s.get("lip"), s.get("lport"), s.get("rip"), s.get("rport"))
                    total = float(s.get("bytesSent") or 0) + float(s.get("bytesReceived") or 0)
                    s["bps"] = _rate(prev.get(key), total, now)
                    current[key] = (now, total)
            for c in report.get("conntrack") or []:
                key = ("c", c.get("proto"), c.get("src"), c.get("sport"), c.get("dst"), c.get("dport"))
                total = float(c.get("bytes") or 0)
                c["bps"] = _rate(prev.get(key), total, now) if c.get("bytes") is not None else 0.0
                current[key] = (now, total)
            self._counters[node] = current
            self._reports[node] = report

    def fresh_reports(self, window_seconds: float) -> list[dict]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [r for r in self._reports.values() if r.get("receivedAt", 0) >= cutoff]

    def status(self, window_seconds: float) -> dict:
        fresh = self.fresh_reports(window_seconds)
        with self._lock:
            known = sorted(self._reports)
        with self._lock:
            agents = [{"node": n, "version": r.get("agentVersion", "?"), "build": r.get("agentBuild", ""),
                       "lastReportS": round(time.time() - r.get("receivedAt", 0), 1)}
                      for n, r in sorted(self._reports.items())]
        return {"nodes": len(fresh), "known": known,
                "stale": sorted(set(known) - {r.get("node") for r in fresh}), "agents": agents}


def _rate(prev: tuple[float, float] | None, total: float, now: float) -> float:
    if not prev:
        return 0.0
    t0, v0 = prev
    dt = now - t0
    if dt <= 0 or total < v0:
        return 0.0
    return (total - v0) / dt


def aggregate(reports: list[dict], resolve: Resolver, pod_for_uid: Callable[[str], str | None],
              max_idle_s: float = 60) -> Observations:
    """Turn raw agent reports into entity-to-entity flow statistics.

    Conntrack entries outlive their flows (dead TCP: up to the established timeout, k3s 24 h; UDP:
    30-120 s), so entries quiet for longer than `max_idle_s` are dropped. Open sockets are always
    current; their idle time is kept so the UI can tell a live call from an idle pooled connection.
    """
    obs = Observations()
    # Pod IPs whose sockets were reported directly: their TCP conntrack entries
    # describe the same connections and would double-count.
    socket_ips = {s.get("lip") for r in reports for n in (r.get("netns") or [])
                  for s in (n.get("sockets") or []) if n.get("sockets")}
    for report in reports:
        obs.nodes_reporting.append(str(report.get("node")))
        obs.capabilities[str(report.get("node"))] = report.get("capabilities") or {}
        for netns in report.get("netns") or []:
            pod_key = pod_for_uid(netns.get("podUid", ""))
            if not pod_key:
                continue
            me: Entity = ("pod", pod_key)
            q = 0
            backlog = 0
            for lst in netns.get("listen") or []:
                q = max(q, int(lst.get("queue") or 0))
                backlog = max(backlog, int(lst.get("backlog") or 0))
            if netns.get("listen"):
                obs.listen[pod_key] = ListenStats(q, backlog)
            for s in netns.get("sockets") or []:
                state = s.get("state", "")
                rip, rport, lport = s.get("rip", ""), s.get("rport"), s.get("lport")
                if s.get("dir") == "in":
                    peer = resolve(rip, None, "tcp")
                    if not peer or state != "ESTAB":
                        continue
                    st = FlowStats(connections_in=1, recv_q=int(s.get("rq") or 0),
                                   bytes_per_sec=float(s.get("bps") or 0), sources={"socket"},
                                   ports={f"{lport}/TCP"}, idle_s=_idle_s(s))
                    obs.add(peer, me, st)
                    continue
                peer = resolve(rip, rport, "tcp")
                if not peer:
                    continue
                st = FlowStats(sources={"socket"}, ports={f"{rport}/TCP"}, idle_s=_idle_s(s))
                if state in ("SYN-SENT", "SYN_SENT"):
                    st.pending = 1
                elif state == "ESTAB":
                    st.connections = 1
                    if s.get("rtt") is not None:
                        st.rtts_ms.append(float(s["rtt"]))
                    if s.get("minrtt") is not None:
                        st.minrtts_ms.append(float(s["minrtt"]))
                    st.retrans = int(s.get("retrans") or 0)
                    st.retrans_total = int(s.get("retransTotal") or 0)
                    st.send_q = int(s.get("sq") or 0)
                    st.bytes_per_sec = float(s.get("bps") or 0)
                else:
                    continue
                obs.add(me, peer, st)
        for c in report.get("conntrack") or []:
            proto = (c.get("proto") or "tcp").lower()
            if proto not in ("tcp", "udp"):
                continue
            src = resolve(c.get("src", ""), None, proto)
            dst = resolve(c.get("dst", ""), _int(c.get("dport")), proto)
            if not src or not dst:
                continue
            idle = c.get("idleS")
            if idle is not None and idle > max_idle_s:
                continue   # the flow ended; its conntrack entry just hasn't expired yet
            st = FlowStats(sources={"conntrack"}, ports={f"{c.get('dport')}/{proto.upper()}"},
                           bytes_per_sec=float(c.get("bps") or 0), idle_s=idle)
            if proto == "udp":
                # UDP has no handshake: no reply yet is normal (pings, telemetry, relays), not a failure.
                st.connections = 1
            elif c.get("unreplied"):
                st.pending = 1       # TCP SYN with no answer: a dead or filtered endpoint
            elif c.get("state") == "ESTABLISHED":
                st.connections = 1
            else:
                continue
            if not (proto == "tcp" and c.get("src") in socket_ips):
                obs.add(src, dst, st)
            # Which backend actually answered a Service VIP / LoadBalancer flow.
            backend = resolve(c.get("rsrc", ""), None, proto)
            if dst[0] == "svc" and backend and backend[0] == "pod" and not c.get("unreplied"):
                obs.add(dst, backend, FlowStats(connections=1, sources={"conntrack"},
                                                ports={f"{c.get('rsport')}/{proto.upper()}"},
                                                bytes_per_sec=float(c.get("bps") or 0)))
    return obs


def _idle_s(sock: dict) -> float | None:
    ms = sock.get("idleMs")
    return None if ms is None else ms / 1000.0


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def merge_counts(flows: dict[tuple[Entity, Entity], FlowStats], key_fn) -> dict:
    """Re-key flows (e.g. pod -> workload) merging stats. key_fn returns None to drop."""
    out: dict = defaultdict(FlowStats)
    for (src, dst), stats in flows.items():
        new = key_fn(src, dst)
        if new is None:
            continue
        out[new].merge(stats)
    return dict(out)
