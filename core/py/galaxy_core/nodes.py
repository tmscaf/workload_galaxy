# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Builders for the graph format the front end draws.

The format grew up describing Kubernetes, so its field names do too: a "workload" with "pods" on a
"host", grouped by "namespace". Every module maps its own world onto those:

    namespace   a group of things drawn as one spiral arm
    workload    one thing in a group: a deployment, a device, a virtual machine
    pod         a part of a workload drawn under it: a pod, an address, a virtual disk
    host        what workloads sit on: a node, a router, a hypervisor
    external    something on the rim the workloads connect to: a service, a datastore, a network
    edge        a connection between two of the above, with how much is flowing

These helpers fill in every key the shared front end and `scale` read, so a module only states
what it knows. Anything of its own goes in under its own key (for example node["vm"]).
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .util import level_for, noisy_or, traffic_level

SEVERITIES = ("critical", "warning", "info")


def blank_risk() -> dict:
    return {"score": 0.0, "level": "ok", "reasons": [], "impact": 0.0, "impactLevel": "ok", "impactFrom": None}


def blank_latency() -> dict:
    return {"rttMinMs": None, "rttP50Ms": None, "rttP95Ms": None, "estimateMs": None, "basis": ""}


def pod(name: str, host_id: str, *, ip: str = "", ready: bool = True, phase: str = "Running", age: int = 0,
        cpu: float | None = None, mem: float | None = None, cpu_limit: float | None = None, mem_limit: float | None = None,
        **extra) -> dict:
    return {"containerInfo": [], "owner": "", "ownerKind": "", "notReadyS": None, "deletingS": None, "claims": [],
            "name": name, "phase": phase, "ready": ready, "restarts": 0, "recentRestarts": 0, "reason": "", "message": "",
            "node": host_id.split(":", 1)[-1], "ip": ip, "hostId": host_id, "hostNetwork": False, "terminal": False,
            "deleting": False, "qos": "", "containers": 1, "cpu": cpu, "mem": mem, "cpuLimit": cpu_limit, "cpuRequest": None,
            "memLimit": mem_limit, "memRequest": None, "ageSeconds": age, "level": "ok", "issues": [], **extra}


def workload(nid: str, name: str, namespace: str, subkind: str, *, pods: list[dict], running: bool = True, age: int = 0,
             cpu_ratio: float | None = None, mem_ratio: float | None = None, idle: bool = False, group: str | None = None,
             **extra) -> dict:
    """`running` False is something known but switched off: drawn dimmed, never counted as failing."""
    n = 1 if running else 0
    return {"id": nid, "kind": "workload", "subkind": subkind, "name": name, "namespace": namespace, "platform": False,
            "replicas": {"desired": n, "ready": n, "current": n}, "pods": pods, "services": [], "images": [],
            "hosts": sorted({p["node"] for p in pods if p.get("node")}), "hpa": None, "completed": False,
            "group": group or namespace,
            "rollout": {"state": "complete", "revision": 1, "ageSeconds": age, "updated": 1, "updatedReady": 1, "desired": 1,
                        "old": 0, "changes": [], "strategy": "", "message": ""},
            "storage": [], "usage": {"cpuRatio": cpu_ratio, "memRatio": mem_ratio}, "idle": idle, "present": running,
            "risk": blank_risk(), "ghost": False, **extra}


def host(hid: str, name: str, role: str, *, cpu_capacity: float = 0.0, mem_capacity: float = 0.0, cpu_used: float = 0.0,
         mem_used: float = 0.0, cpu_requested: float = 0.0, mem_requested: float = 0.0, pod_count: int = 0,
         ready: bool = True, ready_message: str = "", os: str = "", **extra) -> dict:
    """Capacities are in cores and bytes; "requested" is what has been promised to the workloads on it."""
    ratio = lambda a, b: round(a / b, 3) if b else None      # noqa: E731
    return {"id": hid, "kind": "host", "subkind": role, "name": name, "namespace": "", "ready": ready,
            "readyStatus": "True" if ready else "False", "readyMessage": ready_message, "pressure": [], "taints": [], "zone": "",
            "roles": [role.lower()], "cpuAllocatable": cpu_capacity, "memAllocatable": mem_capacity,
            "cpuCapacity": cpu_capacity, "memCapacity": mem_capacity, "cpuUsed": cpu_used, "memUsed": mem_used,
            "podCount": pod_count, "cpuRequested": cpu_requested, "memRequested": mem_requested, "memLimits": mem_requested,
            "podsWithoutMemLimit": 0, "kubelet": "", "os": os, "unschedulable": False,
            "net": {"connections": 0, "retransConnections": 0, "slowConnections": 0, "retransShare": 0.0, "slowShare": 0.0,
                    "reportingNodes": 1, "rttP50Ms": None, "rttP95Ms": None, "clusterRttP50Ms": None},
            "agentStale": False, "cpuRatio": ratio(cpu_used, cpu_capacity), "cpuRequestRatio": ratio(cpu_requested, cpu_capacity),
            "memRatio": ratio(mem_used, mem_capacity), "memRequestRatio": ratio(mem_requested, mem_capacity),
            "memLimitRatio": ratio(mem_requested, mem_capacity), "memSystemReserved": 0.0,
            "risk": {"score": 0.0, "level": "ok", "reasons": []}, "ghost": False, **extra}


def external(nid: str, name: str, subkind: str, *, scope: str, why: str = "", **extra) -> dict:
    return {"id": nid, "kind": "external", "subkind": subkind, "name": name, "namespace": "", "addresses": [], "scope": scope,
            "scopeWhy": why, "risk": blank_risk(), "ghost": False, **extra}


def edge(src: str, dst: str, kind: str, *, bytes_per_sec: float = 0.0, observed: bool = True, connections: int = 1,
         details: list[str] | None = None, ports: list[str] | None = None, **extra) -> dict:
    """`observed` False draws the link dashed: it is configured, but nothing is flowing over it."""
    return {"id": f"{src}->{dst}", "source": src, "target": dst, "type": kind, "evidence": ["observed"] if observed else [],
            "via": [], "ports": ports or [], "details": details or [],
            "traffic": {"connections": connections, "bytesPerSec": round(bytes_per_sec, 1), "lastActiveS": 0 if bytes_per_sec else None,
                        "idle": bytes_per_sec < 50, "level": round(traffic_level(bytes_per_sec), 3), "bytes": 0},
            "latency": blank_latency(), "health": {"retrans": 0, "retransTotal": 0, "sendQ": 0, "recvQ": 0, "pending": 0},
            "risk": {"score": 0.0, "level": "ok", "reasons": []}, **extra}


class Findings:
    """Collects findings and pins each one's weight on the node or edge it is about.

    A finding says what was seen (title, detail), why it matters (cause), what to do (fix), and may
    carry `options`: concrete ways to fix it, each {title, detail, note?, command?}.
    """

    def __init__(self, nodes: dict, edges: dict, pattern_labels: dict[str, str] | None = None):
        self.nodes, self.edges = nodes, edges
        self.items: dict[str, dict] = {}
        self.labels = pattern_labels or {}

    def add(self, category: str, key: str, title: str, *, subject: str = "", severity: str | None = None,
            weight: float | None = None, detail: str = "", cause: str = "", fix: str = "", evidence=(), commands=(),
            since_s: int | None = None, pattern: str | None = None, options: list[dict] | None = None,
            options_note: str = "", **extra) -> dict:
        fid = f"{category}:{key}"
        if fid in self.items:
            return self.items[fid]
        node, edge_ = self.nodes.get(subject), self.edges.get(subject)
        if edge_:
            s, t = self.nodes.get(edge_["source"], {}), self.nodes.get(edge_["target"], {})
            ns = s.get("namespace") or t.get("namespace", "")
            name, kind = f"{s.get('name', '?')} → {t.get('name', '?')}", "call"
        else:
            ns = (node or {}).get("namespace", "")
            name, kind = (node or {}).get("name", ""), (node or {}).get("kind", "")
        sev = severity or ("critical" if (weight or 0) >= 0.6 else "warning" if (weight or 0) >= 0.25 else "info")
        pat = pattern or f"{category}:{key.split(':', 1)[0]}"
        f = {"id": fid, "severity": sev, "category": category, "title": title, "subject": subject if (node or edge_) else "",
             "subjectKind": kind, "subjectName": name, "namespace": ns, "platform": False, "pods": [], "podCount": 0,
             "detail": detail, "cause": cause, "fix": fix, "commands": list(commands)[:6],
             "evidence": [e[:500] for e in evidence if e and e.split(":", 1)[-1].strip()][:6],
             "sinceS": since_s, "root": True, "causedBy": None, "causedByName": None, "blast": 0, "userFacing": False,
             "weight": round(weight or 0, 3), "pattern": pat, "patternLabel": self.labels.get(pat, ""), **extra}
        if options:
            f["options"] = options[:5]
            if options_note:
                f["optionsNote"] = options_note
        self.items[fid] = f
        if weight and (node or edge_):
            (node or edge_).setdefault("diag", []).append({"weight": weight, "text": title, "category": category})
        return f

    def sorted(self) -> list[dict]:
        rank = {s: i for i, s in enumerate(SEVERITIES)}
        return sorted(self.items.values(), key=lambda f: (rank[f["severity"]], -f["weight"]))


def score(objs) -> None:
    """Turn the weights findings pinned on each node and edge into its risk: score, level, reasons."""
    for obj in objs:
        diag = obj.pop("diag", [])
        if not diag:
            continue
        diag.sort(key=lambda d: -d["weight"])
        s = noisy_or([d["weight"] for d in diag])
        obj["risk"].update(score=round(s, 3), level=level_for(s),
                           reasons=[{"text": d["text"], "weight": d["weight"], "category": d["category"]} for d in diag[:6]])
        for p in obj.get("pods", []):
            if not p.get("level_fixed"):
                p["level"] = obj["risk"]["level"]
                p["issues"] = [d["text"] for d in diag[:3]]


def group_rows(nodes: list[dict], findings: list[dict], groups: list[tuple[str, str]], extra=None) -> list[dict]:
    """One summary row per group that has members, in the order given: what the group tiles show."""
    members: dict[str, list[dict]] = defaultdict(list)
    for n in nodes:
        if n["kind"] == "workload":
            members[n["namespace"]].append(n)
    counts: dict[str, Counter] = defaultdict(Counter)
    for f in findings:
        if f["namespace"]:
            counts[f["namespace"]][f["severity"]] += 1
    rows = []
    for gid, title in groups:
        ms = members.get(gid, [])
        if not ms:
            continue
        on = [m for m in ms if m.get("present", True)]
        crit = sum(1 for m in ms if m["risk"]["level"] == "critical")
        warn = sum(1 for m in ms if m["risk"]["level"] == "warning")
        top = next((f for f in findings if f["namespace"] == gid and f["severity"] != "info"), None)
        rows.append({
            "name": gid, "title": title, "group": gid, "platform": False, "workloads": len(ms), "pods": len(on),
            "warning": warn, "critical": crit, "healthy": len(ms) - warn - crit, "degraded": 0, "down": 0,
            "podsReady": len(on), "restarts1h": 0, "rolling": 0,
            "findings": {k: counts[gid].get(k, 0) for k in SEVERITIES},
            "top": {"title": top["title"], "severity": top["severity"], "id": top["id"]} if top else None, "quota": None,
            "online": len(on), "offline": len(ms) - len(on), **(extra(gid, ms) if extra else {})})
    return rows


def hotspots(nodes: dict[str, dict], edges: list[dict], limit: int = 20) -> list[dict]:
    hot = [{"id": n["id"], "kind": n["kind"], "name": n["name"], "namespace": n.get("namespace", ""), "score": n["risk"]["score"],
            "level": n["risk"]["level"], "reasons": n["risk"]["reasons"][:3]} for n in nodes.values() if n["risk"]["level"] != "ok"]
    hot += [{"id": e["id"], "kind": "call", "name": f"{nodes[e['source']]['name']} → {nodes[e['target']]['name']}",
             "namespace": nodes[e["source"]].get("namespace", ""), "score": e["risk"]["score"], "level": e["risk"]["level"],
             "reasons": e["risk"]["reasons"][:3]} for e in edges if e["risk"]["level"] != "ok"]
    hot.sort(key=lambda h: -h["score"])
    return hot[:limit]
