"""Latency / availability risk scoring.

Each node and edge gets an *own* score from signals that point at it directly
(probability-style 0..1, combined with noisy-OR), and workloads also get an
*impact* score inherited from their downstream dependencies. That separates
root causes ("fraud-check is CPU-throttled") from victims ("payments-api is
slow because it waits on fraud-check").
"""
from __future__ import annotations

import math
from collections import defaultdict

from .util import level_for, noisy_or, percentile

BAD_REASONS = {
    "CrashLoopBackOff": 0.85, "ImagePullBackOff": 0.8, "ErrImagePull": 0.8, "CreateContainerConfigError": 0.8,
    "CreateContainerError": 0.8, "OOMKilled": 0.7, "Evicted": 0.5, "Unschedulable": 0.7,
    "ContainerStatusUnknown": 0.5, "RunContainerError": 0.8, "InvalidImageName": 0.8,
}
UNSCHEDULED_ID = "host:(unscheduled)"


def bad_reason(reason: str) -> str | None:
    """The failure a pod reason stands for; init-container failures count like the main container's."""
    base = reason.split(":", 1)[1] if reason.startswith("Init:") else reason
    if base in BAD_REASONS:
        return base
    return "CrashLoopBackOff" if reason.startswith("Init:") and base == "Error" else None
MAX_PATHS = 8
MAX_PATH_DEPTH = 9
MAX_PATH_STEPS = 20000


class _Acc:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def add(self, weight: float, text: str, category: str, code: str = "") -> None:
        """`code` names the signal, so diagnose.py can attach a cause and a next step to it."""
        if weight > 0:
            self.items.append({"weight": round(min(1.0, weight), 3), "text": text, "category": category,
                               "code": code})

    def result(self) -> dict:
        self.items.sort(key=lambda r: r["weight"], reverse=True)
        score = noisy_or([r["weight"] for r in self.items])
        return {"score": round(score, 3), "level": level_for(score), "reasons": self.items}


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def edge_estimate(edge: dict) -> tuple[float | None, str]:
    lat = edge["latency"]
    if lat.get("p95Ms") is not None:
        return lat["p95Ms"], "request p95 (mesh)"
    if lat.get("rttMinMs") is not None:
        return lat["rttMinMs"], "network path RTT"
    if lat.get("connectP50Ms") is not None:
        return lat["connectP50Ms"], "connect p50 (probe)"
    return None, ""


def score_edge(edge: dict, nodes: dict[str, dict]) -> dict:
    acc = _Acc()
    lat, health = edge["latency"], edge["health"]
    target = nodes.get(edge["target"], {})
    source = nodes.get(edge["source"], {})
    external = target.get("kind") == "external"

    p95 = lat.get("p95Ms")
    if p95 is not None:
        if p95 > 1000:
            acc.add(0.9, f"Request p95 {p95:.0f} ms", "latency", "p95")
        elif p95 > 300:
            acc.add(0.65, f"Request p95 {p95:.0f} ms", "latency", "p95")
        elif p95 > 100:
            acc.add(0.35, f"Request p95 {p95:.0f} ms", "latency", "p95")
    err = health.get("errorRate") or 0
    if err > 0.05:
        acc.add(0.8, f"{_pct(err)} of requests fail (5xx)", "errors", "errors")
    elif err > 0.01:
        acc.add(0.45, f"{_pct(err)} of requests fail (5xx)", "errors", "errors")

    # Network path: judged on the kernel's *minimum* RTT. Inside a cluster that floor is well under
    # a millisecond; a high floor means a slow or long path. Internet distance is not a fault, so
    # external calls are judged on retransmits / backpressure only. Smoothed RTT is not scored here:
    # delayed ACKs inflate it on quiet connections (node-relative comparison lives in score_host).
    floor = lat.get("rttMinMs")
    if floor is not None and not external:
        if floor > 10:
            acc.add(0.6, f"Network path RTT {floor:.1f} ms - far too slow for in-cluster traffic", "network", "rtt")
        elif floor > 2:
            acc.add(0.3, f"Network path RTT {floor:.1f} ms - unusually high inside a cluster", "network", "rtt")
    if health.get("retrans"):
        acc.add(0.5, f"{health['retrans']} segment(s) being retransmitted now", "network", "retrans")
    if health.get("sendQ"):
        acc.add(0.45, f"{health['sendQ']} bytes unacknowledged in client send queues - receiver or path is slow",
                "backpressure", "sendq")
    if health.get("pending"):
        acc.add(0.7, f"{health['pending']} connection attempt(s) not answered", "availability", "pending")
    if health.get("probeFailures"):
        acc.add(0.7, f"Connect probe failed {health['probeFailures']}/{health.get('probeAttempts')}", "availability", "probe-fail")
    elif (lat.get("connectP50Ms") or 0) > 10:
        acc.add(0.35, f"Slow TCP connect ({lat['connectP50Ms']:.1f} ms)", "network", "connect-slow")

    if target.get("kind") == "service":
        acc.add(0.85, f"Service {target.get('name')} has no backing pods - calls fail or time out", "availability", "svc-no-pods")
    if target.get("kind") == "workload":
        r = target.get("replicas", {})
        if r.get("desired", 0) > 0 and r.get("ready", 0) == 0 and target.get("subkind") not in ("CronJob", "Job"):
            acc.add(0.6, "Every pod behind this call is unready", "availability", "target-unready")
    if source.get("kind") == "workload" and target.get("kind") == "workload":
        src_zones = {nodes.get(f"host:{h}", {}).get("zone") for h in source.get("hosts", [])} - {"", None}
        dst_zones = {nodes.get(f"host:{h}", {}).get("zone") for h in target.get("hosts", [])} - {"", None}
        if src_zones and dst_zones and not (src_zones & dst_zones):
            acc.add(0.25, f"Crosses zones ({', '.join(sorted(src_zones))} -> {', '.join(sorted(dst_zones))})",
                    "network", "cross-zone")
    for d in edge.get("diag") or []:
        acc.add(d["weight"], d["text"], d["category"], "diag")
    return acc.result()


def score_workload(n: dict, nodes: dict[str, dict], inbound: list[dict], listen: dict | None,
                   probe: dict | None) -> dict:
    acc = _Acc()
    r = n["replicas"]
    desired, ready, current = r.get("desired", 0), r.get("ready", 0), r.get("current", 0)
    batch = n["subkind"] in ("CronJob", "Job")
    if desired == 0 and not batch and n["subkind"] != "DaemonSet":
        n["idle"] = True
    if not batch and desired > 0:
        if ready == 0:
            acc.add(0.9, f"No ready pods (0/{desired})" if current else f"No pods running (0/{desired})",
                    "availability", "replicas")
        elif ready < desired:
            acc.add(0.25 + 0.5 * (1 - ready / desired), f"{ready}/{desired} pods ready", "availability", "replicas")

    reasons: dict[str, int] = defaultdict(int)
    recent = 0
    cpu_ratio = mem_ratio = 0.0
    unscheduled: dict[str, int] = defaultdict(int)
    evicted = 0
    for p in n["pods"]:
        if p["terminal"]:
            evicted += 1 if p["reason"] == "Evicted" else 0
            continue
        if p["phase"] == "Pending" and not p["node"]:
            unscheduled[p["message"] or p["reason"]] += 1
            continue
        if bad_reason(p["reason"]):
            reasons[p["reason"]] += 1
        recent += p.get("recentRestarts", 0)
        if p.get("cpu") is not None and p.get("cpuLimit"):
            cpu_ratio = max(cpu_ratio, p["cpu"] / p["cpuLimit"])
        if p.get("mem") is not None and p.get("memLimit"):
            mem_ratio = max(mem_ratio, p["mem"] / p["memLimit"])
    for reason, count in reasons.items():
        acc.add(BAD_REASONS[bad_reason(reason)], f"{count} pod(s) in {reason}", "availability",
                f"pod:{bad_reason(reason)}")
    for msg, count in unscheduled.items():
        acc.add(0.7, f"{count} pod(s) can't be scheduled: {msg}", "scheduling", "unschedulable")
    if evicted:
        acc.add(0.4, f"{evicted} pod(s) evicted by the kubelet (node pressure)", "stability", "evicted")
    if recent:
        acc.add(min(0.6, 0.2 + 0.1 * recent), f"{recent} container restart(s) in the last hour", "stability",
                "restarts")
    if cpu_ratio >= 0.9:
        acc.add(0.7, f"CPU at {_pct(cpu_ratio)} of limit - CFS throttling adds latency", "saturation", "cpu")
    elif cpu_ratio >= 0.75:
        acc.add(0.4, f"CPU at {_pct(cpu_ratio)} of limit", "saturation", "cpu")
    if mem_ratio >= 0.9:
        acc.add(0.6, f"Memory at {_pct(mem_ratio)} of limit - OOM kills / GC pauses likely", "saturation", "mem")
    elif mem_ratio >= 0.8:
        acc.add(0.3, f"Memory at {_pct(mem_ratio)} of limit", "saturation", "mem")
    n["usage"] = {"cpuRatio": round(cpu_ratio, 3) if cpu_ratio else None,
                  "memRatio": round(mem_ratio, 3) if mem_ratio else None}

    hpa = n.get("hpa")
    if hpa and hpa.get("max") and (hpa.get("current") or 0) >= hpa["max"]:
        acc.add(0.5, f"Autoscaler pinned at max ({hpa['max']} replicas)", "saturation", "hpa-max")
    if listen and listen.get("queue"):
        q, b = listen["queue"], listen.get("backlog") or 0
        ratio = q / b if b else 0
        acc.add(min(0.9, 0.45 + ratio), f"{q} connection(s) waiting in the accept queue (backlog {b})", "saturation",
                "accept-queue")
    recv_q = sum(e["health"].get("recvQ") or 0 for e in inbound)
    if recv_q:
        acc.add(0.45, f"{recv_q} bytes received but not yet read by the app - it is not keeping up", "saturation", "recvq")
    if probe and probe.get("attempts"):
        fails = probe["failures"]
        if fails:
            acc.add(0.6, f"{fails}/{probe['attempts']} connect probes to pods failed", "availability", "probe-fail")
        p95 = percentile(probe["samples"], 0.95)
        if p95 is not None and p95 > 20:
            acc.add(0.4, f"Pods slow to accept connections (p95 {p95:.1f} ms)", "saturation", "probe-slow")
    callers = {e["source"] for e in inbound if e["type"] == "calls"}
    if desired == 1 and len(callers) >= 3 and not batch:
        acc.add(0.2, f"Single replica serving {len(callers)} callers", "capacity", "single-replica")
    # What diagnose.py found that pod states alone don't show: stuck rollouts, quota, storage, wiring.
    for d in n.get("diag") or []:
        acc.add(d["weight"], d["text"], d["category"], "diag")
    # Node trouble is attributed through impact (impactFrom = the host), not as the workload's own fault.
    return acc.result()


PRESSURE_TEXT = {
    "MemoryPressure": "MemoryPressure - kubelet is evicting pods to reclaim memory",
    "DiskPressure": "DiskPressure - kubelet is evicting pods / garbage-collecting images",
    "PIDPressure": "PIDPressure - process table nearly full",
    "NetworkUnavailable": "NetworkUnavailable - pod network not configured on this node",
}


def _gib(b: float | None) -> str:
    return "?" if b is None else f"{b / 1024 ** 3:.1f} GiB"


def score_host(n: dict) -> dict:
    acc = _Acc()
    if n.get("pseudo"):
        pending = n.get("pending") or []
        msgs: dict[str, int] = defaultdict(int)
        for p in pending:
            msgs[p["message"] or p["reason"]] += 1
        for msg, count in sorted(msgs.items(), key=lambda kv: -kv[1]):
            acc.add(0.9 if count == len(pending) else 0.7, f"{count} pod(s): {msg}", "scheduling", "sched-queue")
        return acc.result()
    if not n.get("ready"):
        acc.add(0.95, f"Node {'unreachable' if n.get('readyStatus') == 'Unknown' else 'NotReady'}"
                      f"{' - ' + n['readyMessage'] if n.get('readyMessage') else ''}", "infrastructure", "notready")
    for p in n.get("pressure") or []:
        acc.add(0.7, PRESSURE_TEXT.get(p, p), "infrastructure", f"pressure:{p}")
    cpu_a, mem_a = n.get("cpuAllocatable"), n.get("memAllocatable")
    if cpu_a:
        n["cpuRatio"] = round(n.get("cpuUsed", 0) / cpu_a, 3)
        n["cpuRequestRatio"] = round(n.get("cpuRequested", 0) / cpu_a, 3)
        if n["cpuRatio"] > 0.9:
            acc.add(0.6, f"Pods use {_pct(n['cpuRatio'])} of node CPU - every pod here competes for cycles", "saturation", "node-cpu")
        elif n["cpuRatio"] > 0.75:
            acc.add(0.3, f"Pods use {_pct(n['cpuRatio'])} of node CPU", "saturation", "node-cpu")
        if n["cpuRequestRatio"] >= 0.9:
            acc.add(0.35, f"CPU requests reserve {_pct(n['cpuRequestRatio'])} of allocatable", "reservations", "node-cpu-req")
    if mem_a:
        n["memRatio"] = round(n.get("memUsed", 0) / mem_a, 3)
        n["memRequestRatio"] = round(n.get("memRequested", 0) / mem_a, 3)
        n["memLimitRatio"] = round(n.get("memLimits", 0) / mem_a, 3)
        if n["memRatio"] > 0.9:
            acc.add(0.6, f"Pods use {_pct(n['memRatio'])} of node memory", "saturation", "node-mem")
        elif n["memRatio"] > 0.8:
            acc.add(0.3, f"Pods use {_pct(n['memRatio'])} of node memory", "saturation", "node-mem")
        free = mem_a - n.get("memRequested", 0)
        if n["memRequestRatio"] >= 0.98:
            acc.add(0.6, f"Memory requests reserve {_pct(n['memRequestRatio'])} of allocatable "
                         f"({_gib(free)} free) - nothing more can be scheduled here", "reservations", "node-mem-req")
        elif n["memRequestRatio"] >= 0.9:
            acc.add(0.45, f"Memory requests reserve {_pct(n['memRequestRatio'])} of allocatable "
                          f"({_gib(free)} free) - little room to reschedule pods", "reservations", "node-mem-req")
        if n["memLimitRatio"] > 1.5:
            acc.add(0.3, f"Memory limits overcommitted {n['memLimitRatio']:.1f}x allocatable - "
                         "OOM kills if pods grow toward their limits", "reservations", "node-mem-limits")
    if n.get("memCapacity") and mem_a:
        n["memSystemReserved"] = max(0.0, n["memCapacity"] - mem_a)
    net = n.get("net") or {}
    if net.get("connections"):
        # With several reporting nodes, localise: this node must own most of the cluster's anomalies.
        multi = (net.get("reportingNodes") or 1) >= 2
        own = net["retransConnections"] / net["connections"]
        if net["retransConnections"] >= 3 and ((multi and net["retransShare"] >= 0.6) or (not multi and own >= 0.2)):
            acc.add(0.7 if own >= 0.2 else 0.5,
                    f"Retransmits on {net['retransConnections']} of {net['connections']} connections from pods on "
                    f"this node ({_pct(net['retransShare'])} of the cluster's) - packet loss", "network", "node-retrans")
        # "Slower than the same calls from other nodes" only means something when there ARE other
        # nodes; on a single node it compares delayed-ACK noise with itself. Demand a clear pattern.
        slow = net.get("slowConnections", 0)
        if multi and slow >= 5 and net["slowShare"] >= 0.6 and slow / net["connections"] >= 0.2:
            acc.add(0.55, f"{net['slowConnections']} connections from this node are >3x slower than the same calls "
                          f"from other nodes (RTT p95 {net.get('rttP95Ms') or 0:.1f} ms)", "network", "node-slow")
    if n.get("agentStale"):
        acc.add(0.3, "Flow agent on this node stopped reporting", "telemetry", "agent-stale")
    if n.get("unschedulable"):
        acc.add(0.2, "Cordoned (unschedulable)", "infrastructure", "cordoned")
    custom = [t for t in n.get("taints") or [] if not t.startswith("node.kubernetes.io/")
              and not t.startswith("node-role.kubernetes.io/") and t.endswith((":NoSchedule", ":NoExecute"))]
    if custom:
        acc.add(0.1, f"Tainted {', '.join(custom)} - only tolerating pods land here", "scheduling", "taint")
    return acc.result()


def score_pod(p: dict, host: dict | None) -> None:
    """Per-pod level + human-readable issues (shown on the pods plane and in the drawer)."""
    issues: list[str] = []
    level = "ok"

    def bump(new: str, text: str) -> None:
        nonlocal level
        order = {"ok": 0, "warning": 1, "critical": 2}
        if order[new] > order[level]:
            level = new
        issues.append(text)

    if p["reason"] == "Evicted":
        p["level"] = "evicted"
        p["issues"] = [f"Evicted: {p['message'] or 'node pressure'}"]
        return
    if p["terminal"]:
        p["level"] = "evicted"
        p["issues"] = [f"{p['phase']}: {p['reason'] or p['message']}"]
        return
    if p["phase"] == "Pending" and not p["node"]:
        bump("critical", f"Unschedulable: {p['message'] or p['reason']}")
    elif bad_reason(p["reason"]):
        bump("critical", p["reason"] + (f": {p['message']}" if p["message"] else ""))
    if host and not host.get("pseudo") and not host.get("ready"):
        bump("critical", f"On {host['name']}, which is {'unreachable' if host.get('readyStatus') == 'Unknown' else 'NotReady'}")
    if not p["ready"] and p["phase"] == "Running" and level == "ok":
        bump("warning", "Not ready")
    if p["phase"] == "Pending" and p["node"] and level == "ok":
        bump("warning", f"Starting ({p['reason'] or 'ContainerCreating'})")
    if p.get("recentRestarts"):
        bump("warning", f"{p['recentRestarts']} restart(s) in the last hour")
    if p.get("cpu") is not None and p.get("cpuLimit"):
        if p["cpu"] / p["cpuLimit"] >= 0.9:
            bump("warning", f"CPU at {_pct(p['cpu'] / p['cpuLimit'])} of limit (throttled)")
    if p.get("mem") is not None and p.get("memLimit"):
        if p["mem"] / p["memLimit"] >= 0.9:
            bump("warning", f"Memory at {_pct(p['mem'] / p['memLimit'])} of limit")
    if (p.get("cpu") or 0) >= 2 and not p.get("cpuLimit") and host and (host.get("cpuRatio") or 0) > 0.8:
        bump("warning", f"Noisy neighbour: using {p['cpu']:.1f} cores with no CPU limit on a busy node")
    if p.get("deleting"):
        bump("warning", "Terminating")
    p["level"] = level
    p["issues"] = issues


def score_graph(graph: dict, *, listen: dict | None = None, node_probe: dict | None = None) -> None:
    nodes = {n["id"]: n for n in graph["nodes"]}
    inbound: dict[str, list[dict]] = defaultdict(list)
    outbound: dict[str, list[dict]] = defaultdict(list)
    for e in graph["edges"]:
        inbound[e["target"]].append(e)
        outbound[e["source"]].append(e)
        est, basis = edge_estimate(e)
        e["latency"]["estimateMs"] = est
        e["latency"]["basis"] = basis

    for n in graph["nodes"]:
        if n["kind"] == "host":
            n["risk"] = score_host(n)
    for n in graph["nodes"]:
        if n["kind"] == "workload":
            for p in n["pods"]:
                score_pod(p, nodes.get(p.get("hostId", "")))
    for n in graph["nodes"]:
        if n["kind"] == "workload":
            n["risk"] = score_workload(n, nodes, inbound[n["id"]], (listen or {}).get(n["id"]),
                                       (node_probe or {}).get(n["id"]))
        elif n["kind"] == "service":
            n["risk"] = {"score": 0.85, "level": "critical",
                         "reasons": [{"weight": 0.85, "text": "Selector matches no ready pods", "category": "availability",
                                      "code": "svc-no-pods"}]}
        elif n["kind"] != "host":
            acc = _Acc()                       # routes / ingresses: wiring problems found by diagnose.py
            for d in n.get("diag") or []:
                acc.add(d["weight"], d["text"], d["category"], "diag")
            n["risk"] = acc.result()
    for e in graph["edges"]:
        e["risk"] = score_edge(e, nodes)

    # Impact: how much trouble flows back up to a node from what it depends on.
    host_of = {n["id"]: [f"host:{h}" for h in n.get("hosts", []) if f"host:{h}" in nodes]
               for n in graph["nodes"] if n["kind"] == "workload"}
    for n in graph["nodes"]:
        if n["kind"] == "workload" and any(p["phase"] == "Pending" and not p["node"] for p in n["pods"]) \
                and UNSCHEDULED_ID in nodes:
            host_of[n["id"]].append(UNSCHEDULED_ID)
    impact = {nid: 0.0 for nid in nodes}
    source_of = {nid: None for nid in nodes}
    for _ in range(12):
        changed = False
        for nid in nodes:
            # Recomputed from scratch each pass so attribution settles regardless of visit order.
            best, frm = 0.0, None
            for e in outbound[nid]:
                t = e["target"]
                # Candidates, deepest first: what the callee depends on, the callee, the call itself.
                cands = [(impact[t] * 0.7, source_of[t]), (nodes[t]["risk"]["score"] * 0.7, t),
                         (e["risk"]["score"] * 0.85, e["id"])]
                value = max(v for v, _ in cands)
                if value <= best + 1e-6:
                    continue
                # Slow calls are usually a symptom of trouble further down: blame the deepest
                # cause that explains a comparable share of the risk.
                root = next(r for v, r in cands if r and v >= 0.6 * value)
                best, frm = value, root
            # The node a workload runs on is a dependency too (pressure, NotReady, bad NIC).
            for h in host_of.get(nid, ()):
                value = nodes[h]["risk"]["score"] * 0.75
                if value > best + 1e-6:
                    best, frm = value, h
            if abs(best - impact[nid]) > 1e-6 or frm != source_of[nid]:
                impact[nid], source_of[nid] = best, frm
                changed = True
        if not changed:
            break
    for nid, n in nodes.items():
        if n["kind"] == "host":
            continue
        n["risk"]["impact"] = round(impact[nid], 3)
        n["risk"]["impactLevel"] = level_for(impact[nid])
        n["risk"]["impactFrom"] = source_of[nid]

    # Hotspots: the things with their own problems, worst first.
    hot = []
    for n in graph["nodes"]:
        if n["risk"]["score"] >= 0.25:
            hot.append({"id": n["id"], "type": "node", "name": n["name"], "namespace": n.get("namespace", ""),
                        "kind": n["kind"], "score": n["risk"]["score"], "level": n["risk"]["level"],
                        "reasons": [r["text"] for r in n["risk"]["reasons"][:3]]})
    for e in graph["edges"]:
        if e["risk"]["score"] >= 0.25:
            hot.append({"id": e["id"], "type": "edge", "name": f"{nodes[e['source']]['name']} -> {nodes[e['target']]['name']}",
                        "namespace": nodes[e["target"]].get("namespace", ""), "kind": "edge",
                        "score": e["risk"]["score"], "level": e["risk"]["level"],
                        "reasons": [r["text"] for r in e["risk"]["reasons"][:3]]})
    hot.sort(key=lambda h: h["score"], reverse=True)
    graph["hotspots"] = hot[:25]
    graph["paths"] = _critical_paths(graph, nodes, outbound, inbound)
    graph["cascades"] = _cascades(graph, nodes, inbound, host_of)


def _cascades(graph: dict, nodes: dict, inbound: dict, host_of: dict) -> list[dict]:
    """Failure cascades: for every root cause, everything upstream that depends on it and is degraded.

    `impacted` is the blast radius (callers, their callers, ... while they are degraded); `dominant`
    is the subset for which this root is the *main* cause (risk.impactFrom).
    """
    edges = {e["id"]: e for e in graph["edges"]}
    on_host: dict[str, list[str]] = defaultdict(list)
    for wid, hosts in host_of.items():
        for h in hosts:
            on_host[h].append(wid)
    roots = {n["risk"]["impactFrom"] for n in graph["nodes"]
             if n["kind"] != "host" and n["risk"].get("impactLevel", "ok") != "ok" and n["risk"].get("impactFrom")}

    def degraded(nid: str) -> bool:
        r = nodes[nid].get("risk") or {}
        return r.get("impactLevel", "ok") != "ok" or r.get("level", "ok") != "ok"

    out = []
    for root in roots:
        if root in nodes:
            rn = nodes[root]
            kind, name, score, level = rn["kind"], rn["name"], rn["risk"]["score"], rn["risk"]["level"]
            reasons = [x["text"] for x in rn["risk"]["reasons"][:2]]
            start = on_host.get(root, []) if rn["kind"] == "host" else [e["source"] for e in inbound.get(root, [])]
        elif root in edges:
            e = edges[root]
            kind, score, level = "call", e["risk"]["score"], e["risk"]["level"]
            name = f"{nodes[e['source']]['name']} -> {nodes[e['target']]['name']}"
            reasons = [x["text"] for x in e["risk"]["reasons"][:2]]
            start = [e["source"]]
        else:
            continue
        seen: set[str] = set()
        stack = [s for s in start if s in nodes and degraded(s)]
        while stack:
            nid = stack.pop()
            if nid in seen or nid == root:
                continue
            seen.add(nid)
            stack.extend(e["source"] for e in inbound.get(nid, []) if degraded(e["source"]))
        if not seen:
            continue
        user_facing = any(nodes[x]["kind"] in ("clients", "entry") for x in seen)
        seen = {x for x in seen if nodes[x]["kind"] != "clients"}
        dominant = sorted(x for x in seen if nodes[x]["risk"].get("impactFrom") == root)
        out.append({"root": root, "kind": kind, "name": name, "score": score, "level": level, "reasons": reasons,
                    "impacted": sorted(seen), "dominant": dominant, "count": len(seen), "userFacing": user_facing,
                    # severity x reach: a critical root with a wide blast radius comes first
                    "weight": round(score * (1 + math.log2(1 + len(seen))), 3)})
    out.sort(key=lambda c: (c["userFacing"], c["weight"]), reverse=True)
    return out


def _critical_paths(graph: dict, nodes: dict, outbound: dict, inbound: dict) -> list[dict]:
    starts = [nid for nid, n in nodes.items() if n["kind"] == "clients"]
    if not starts:
        starts = [nid for nid, n in nodes.items() if n["kind"] in ("entry", "workload") and not inbound[nid]]
    paths: list[dict] = []
    steps = 0

    def walk(nid: str, trail_nodes: list[str], trail_edges: list[dict]) -> None:
        nonlocal steps
        steps += 1
        if steps > MAX_PATH_STEPS:
            return
        nexts = [e for e in outbound[nid] if e["target"] not in trail_nodes]
        if not nexts or len(trail_nodes) >= MAX_PATH_DEPTH:
            if len(trail_edges) >= 1:
                scores = [nodes[x]["risk"]["score"] for x in trail_nodes] + [e["risk"]["score"] for e in trail_edges]
                lat = [e["latency"]["estimateMs"] for e in trail_edges if e["latency"].get("estimateMs") is not None]
                # A request path is as bad as its worst hop.
                paths.append({"nodes": list(trail_nodes), "edges": [e["id"] for e in trail_edges],
                              "score": round(max(scores), 3), "latencyMs": round(sum(lat), 2) if lat else None,
                              "measuredHops": len(lat), "hops": len(trail_edges)})
            return
        for e in nexts:
            walk(e["target"], trail_nodes + [e["target"]], trail_edges + [e])

    for s in starts:
        walk(s, [s], [])
    paths.sort(key=lambda p: (p["score"], p["latencyMs"] or 0), reverse=True)
    seen_ends = set()
    out = []
    for p in paths:
        key = (p["nodes"][-1], p["score"])
        if key in seen_ends:
            continue
        seen_ends.add(key)
        out.append(p)
        if len(out) >= MAX_PATHS:
            break
    return out
