# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Keeping the picture drawable at any size.

A cluster with 12,000 workloads cannot be drawn one star per workload, and the
browser should not download it either. This module turns a (filtered) graph into
what one screen can show:

    collapse()   semantic zoom. Above `detail_limit` workloads the unit becomes the
                 project; above `project_limit` projects it becomes the business unit
                 (namespace prefix or label). Units the user expanded are shown in
                 detail, and everything stays under `budget` nodes.
    An aggregate is served as a workload-shaped node (kind "workload", `aggregate`
    set) whose "pods" are its children, so every view draws it without special
    cases: a star with its workloads in orbit, a tower with a floor per workload.

    workload_rows() / call_rows() / node_detail() / search()
                 the tables, drawer and search work against the full graph on the
                 server, paged, instead of against whatever the page happens to hold.

Small clusters pass through collapse() untouched.
"""
from __future__ import annotations

from collections import Counter, defaultdict

LEVEL_RANK = {"ok": 0, "warning": 1, "critical": 2}
MAX_FLOORS = 14          # children drawn per aggregate (pods in orbit / tower floors)
MAX_CHILDREN = 60        # children listed in the drawer
MAX_HOSTS = 24           # nodes drawn individually before healthy ones group by zone


def _worst(levels) -> str:
    return max(levels, key=lambda lv: LEVEL_RANK.get(lv, 0), default="ok")


def _share_level(total: int, failing: int, down: int, *, small: bool) -> str:
    """Colour of an aggregate: how much of it is in trouble, not merely whether anything is. One
    failing workload among 700 makes a unit amber (and is listed in Triage), not red."""
    if not failing or not total:
        return "ok"
    if small:
        return "critical" if down or failing / total >= 0.15 else "warning"
    return "critical" if failing / total >= 0.10 or down / total >= 0.03 else "warning"


def _state(n: dict) -> str:
    r = n.get("replicas") or {}
    if r.get("desired", 0) > 0 and r.get("ready", 0) == 0 and n.get("subkind") not in ("CronJob", "Job"):
        return "down"
    return "degraded" if (n.get("risk") or {}).get("level", "ok") != "ok" else "healthy"


def collapse(graph: dict, *, expand: list[str] | None = None, level: str = "auto", detail_limit: int = 150,
             project_limit: int = 60, budget: int = 320, focus: bool = False) -> tuple[dict, dict[str, str]]:
    """-> (graph to serve, {real node id: id it is drawn as}).

    `focus`: problems only. What is failing is drawn individually together with what calls it and what
    it depends on; everything else is rolled up, whatever the size of the cluster."""
    nodes = graph["nodes"]
    real = [n for n in nodes if n["kind"] in ("workload", "entry", "service") and n.get("namespace")]
    workloads = [n for n in real if n["kind"] == "workload" and not n.get("ghost")]
    projects = sorted({n["namespace"] for n in real})
    group_of = {n["namespace"]: n.get("group") or n["namespace"] for n in real}
    groups = sorted(set(group_of.values()))
    view = {"level": "workloads", "collapsed": False, "workloads": len(workloads), "projects": len(projects),
            "units": len(groups), "expanded": [], "dropped": [], "budget": budget, "focus": False}
    same = {n["id"]: n["id"] for n in nodes}
    if not focus and (level == "workloads" or (level == "auto" and len(workloads) <= detail_limit)):
        for f in graph.get("findings", []) + [x for p in graph.get("patterns", []) for x in p["examples"]]:
            f["at"] = f["subject"]
        return {**graph, "view": {**view, "shown": len(nodes)}}, same

    base = "projects" if level == "projects" or (level != "units" and len(projects) <= project_limit) or len(groups) < 2 \
        else "units"
    expand = [x for x in (expand or []) if x.startswith(("grp:", "ns:"))]
    ns_count = Counter(n["namespace"] for n in real)
    group_projects: dict[str, set[str]] = defaultdict(set)
    for ns, g in group_of.items():
        group_projects[g].add(ns)

    def plan(items: list[str]):
        open_groups = {x[4:] for x in items if x.startswith("grp:")}
        open_ns = {x[3:] for x in items if x.startswith("ns:")}
        shown = 0
        for g in groups:
            if base == "projects" or g in open_groups or any(ns in open_ns for ns in group_projects[g]):
                shown += sum(ns_count[ns] if ns in open_ns else 1 for ns in group_projects[g])
            else:
                shown += 1
        return open_groups, open_ns, shown

    # Problems-only: the worst failing things first, each with its direct callers and dependencies,
    # for as many as one screen can hold. The rest stay inside their unit, which still counts them.
    detail: set[str] = set()
    if focus:
        by_id = {n["id"]: n for n in real}
        near: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for e in graph["edges"]:
            w = e["risk"]["score"] + (e["traffic"].get("level") or 0) * 0.1
            # Real calls only: a link merely inferred from config (every pod's telemetry endpoint, say)
            # would pull shared infrastructure into every neighbourhood.
            seen = e["risk"]["level"] != "ok" or any(x in e["evidence"] for x in ("observed", "mesh", "declared"))
            if seen and e["source"] in by_id and e["target"] in by_id:
                near[e["source"]].append((w, e["target"]))
                near[e["target"]].append((w, e["source"]))
        failing = sorted((n for n in real if n["risk"]["level"] != "ok" and not n.get("ghost")),
                         key=lambda n: (-LEVEL_RANK[n["risk"]["level"]], -n["risk"]["score"], n["id"]))
        room = max(30, min(140, budget - len(groups) - 70))      # more than this stops being readable
        shown_problems = 0
        for n in failing:
            add = {n["id"]} | {x for _w, x in sorted(near[n["id"]], reverse=True)[:6]}
            if len(detail | add) > room:
                break
            detail |= add
            shown_problems += 1
        view.update(focus=True, focusShown=shown_problems, focusTotal=len(failing),
                    focusRest=len(workloads) - sum(1 for n in workloads if n["id"] in detail))
        ns_count = Counter(n["namespace"] for n in real if n["id"] not in detail)

    other = len(nodes) - len(real)
    dropped: list[str] = []
    open_groups, open_ns, shown = plan(expand)
    while expand and shown + len(detail) + min(other, MAX_HOSTS + 30) > budget:     # oldest expansion gives way first
        dropped.append(expand.pop(0))
        open_groups, open_ns, shown = plan(expand)

    def container(n: dict) -> str:
        ns = n["namespace"]
        if ns in open_ns or n["id"] in detail:
            return n["id"]
        g = group_of[ns]
        if base == "projects" or g in open_groups or any(x in open_ns for x in group_projects[g]):
            return f"proj:{ns}"
        return f"grp:{g}"

    cmap: dict[str, str] = {}
    members: dict[str, list[dict]] = defaultdict(list)
    out_nodes: list[dict] = []
    for n in nodes:
        if n["kind"] in ("workload", "entry", "service") and n.get("namespace"):
            cid = container(n)
            cmap[n["id"]] = cid
            if cid == n["id"]:
                out_nodes.append(n)
            else:
                members[cid].append(n)
        elif n["kind"] != "host":
            cmap[n["id"]] = n["id"]
            out_nodes.append(n)
    hosts = _group_hosts([n for n in nodes if n["kind"] == "host"], cmap)

    findings = graph.get("findings", [])
    top_finding: dict[str, dict] = {}
    for f in graph.get("_scoped") or findings:     # every finding in scope, not just the ones being sent
        if f["subject"] and f["severity"] != "info" and f["root"] and not f.get("effectOf"):
            top_finding.setdefault(f["subject"], f)
    for cid, ms in members.items():
        out_nodes.append(_aggregate(cid, ms, cmap, top_finding, group_of))
    out_nodes += hosts

    edges, emap = _merge_edges(graph["edges"], cmap, {n["id"]: n for n in out_nodes})
    if focus:      # the lines that matter are the ones touching a problem; unit-to-unit bundles wait for a click
        for e in edges:
            if e["source"].startswith(("proj:", "grp:")) and e["target"].startswith(("proj:", "grp:")):
                e["minor"] = True
    visible = {n["id"] for n in out_nodes} | {e["id"] for e in edges}
    examples = [x for p in graph.get("patterns", []) for x in p["examples"]]
    for f in findings + examples:
        f["at"] = cmap.get(f["subject"]) or emap.get(f["subject"]) or (f["subject"] if f["subject"] in visible else "")

    cascades, seen_roots = [], set()
    for c in graph.get("cascades", []):
        root = cmap.get(c["root"]) or emap.get(c["root"])
        if not root or root in seen_roots:
            continue
        seen_roots.add(root)
        impacted = sorted({cmap[x] for x in c["impacted"] if x in cmap} - {root})
        dominant = sorted({cmap[x] for x in c.get("dominant", []) if x in cmap} - {root})
        if impacted:
            cascades.append({**c, "root": root, "impacted": impacted, "dominant": dominant,
                             "name": next((n["name"] for n in out_nodes if n["id"] == root), c["name"])})
    hot = [{"id": n["id"], "type": "node", "name": n["name"], "namespace": n.get("project") or n.get("namespace", ""),
            "kind": n["kind"], "score": n["risk"]["score"], "level": n["risk"]["level"],
            "reasons": [r["text"] for r in n["risk"]["reasons"][:3]]}
           for n in out_nodes if (n.get("risk") or {}).get("score", 0) >= 0.25]
    hot.sort(key=lambda h: h["score"], reverse=True)
    view.update(level=base, collapsed=True, expanded=expand, dropped=dropped, shown=len(out_nodes))
    out = {**graph, "nodes": out_nodes, "edges": edges, "cascades": cascades[:20], "hotspots": hot[:25], "paths": [],
           "view": view}
    return out, cmap


def _aggregate(cid: str, ms: list[dict], cmap: dict[str, str], top_finding: dict[str, dict], group_of: dict) -> dict:
    is_group = cid.startswith("grp:")
    wls = [m for m in ms if m["kind"] == "workload"]
    states = Counter(_state(m) for m in wls)
    if is_group:
        by_ns: dict[str, list[dict]] = defaultdict(list)
        for m in ms:
            by_ns[m["namespace"]].append(m)
        children = []
        for ns, items in by_ns.items():
            w = [m for m in items if m["kind"] == "workload"]
            st = Counter(_state(m) for m in w)
            worst = max(items, key=lambda m: m["risk"]["score"])
            f = top_finding.get(worst["id"])
            lvl = _share_level(len(w), st["down"] + st["degraded"], st["down"], small=True)
            children.append({"id": f"proj:{ns}", "expand": f"ns:{ns}", "name": ns, "kind": "Project",
                             "level": lvl, "score": worst["risk"]["score"] if lvl == "critical" else min(0.5, worst["risk"]["score"]),
                             "ready": f"{st['healthy']}/{len(w)} healthy", "workloads": len(w),
                             "issue": f"{worst['name']}: {f['title']}" if f else ""})
    else:
        children = []
        for m in ms:
            f = top_finding.get(m["id"])
            r = m.get("replicas") or {}
            children.append({"id": m["id"], "name": m["name"], "kind": m.get("subkind") or m["kind"],
                             "level": m["risk"]["level"], "score": m["risk"]["score"],
                             "ready": f"{r.get('ready', 0)}/{r.get('desired', 0)}" if m["kind"] == "workload" else "",
                             "issue": f["title"] if f else (m["risk"]["reasons"][0]["text"] if m["risk"]["reasons"] else "")})
    children.sort(key=lambda c: (-c["score"], c["name"]))
    worst_member = max(ms, key=lambda m: m["risk"]["score"])
    impact_member = max(ms, key=lambda m: m["risk"].get("impact") or 0)
    impact_from = cmap.get(impact_member["risk"].get("impactFrom") or "")
    outside = impact_from and impact_from != cid
    name = cid.split(":", 1)[1]
    ns0 = ms[0]["namespace"]
    reasons = [{"weight": c["score"], "text": f"{c['name']}: {c['issue']}", "category": "member", "code": "member"}
               for c in children[:3] if c["issue"] and c["score"] >= 0.25]
    failing = states["down"] + states["degraded"]
    level = _share_level(len(wls), failing, states["down"], small=not is_group) if wls else _worst(m["risk"]["level"] for m in ms)
    score = worst_member["risk"]["score"] if level == "critical" else min(0.5, worst_member["risk"]["score"]) if level == "warning" else 0.0
    # Children stand in for pods: one hexagon in orbit / one tower floor each, worst first.
    pods = [{"name": c["name"], "child": c["id"], "expand": c.get("expand"), "pseudo": True, "level": c["level"],
             "phase": "Running", "ready": c["level"] == "ok", "reason": "", "message": "", "node": "", "hostId": "",
             "terminal": False, "restarts": 0, "recentRestarts": 0, "issues": [c["issue"]] if c["issue"] else [],
             "summary": c["ready"]} for c in children[:MAX_FLOORS if is_group else MAX_FLOORS // 2]]
    return {
        "id": cid, "kind": "workload", "subkind": "Business unit" if is_group else "Project",
        "aggregate": "unit" if is_group else "project", "expand": cid if is_group else f"ns:{ns0}",
        "name": name, "namespace": group_of.get(ns0, ns0), "project": None if is_group else ns0,
        "group": group_of.get(ns0, ns0), "platform": all(m.get("platform") for m in ms),
        "ghost": all(m.get("ghost") for m in ms),
        "replicas": {"desired": len(wls), "ready": states["healthy"], "current": len(wls)},
        "counts": {"projects": len({m["namespace"] for m in ms}), "workloads": len(wls), "members": len(ms),
                   "healthy": states["healthy"],
                   "degraded": states["degraded"], "down": states["down"], "failing": failing,
                   "pods": sum(len(m.get("pods") or []) for m in wls)},
        "pods": pods, "children": children[:MAX_CHILDREN], "childrenTotal": len(children),
        "images": [], "services": [], "hosts": [], "hpa": None, "usage": {},
        "risk": {"score": score, "level": level, "reasons": reasons if level != "ok" else [], "impact": (impact_member["risk"].get("impact") or 0) if outside else 0,
                 "impactLevel": impact_member["risk"].get("impactLevel", "ok") if outside else "ok",
                 "impactFrom": impact_from if outside else None},
    }


def _group_hosts(hosts: list[dict], cmap: dict[str, str]) -> list[dict]:
    real = [h for h in hosts if not h.get("pseudo")]
    if len(real) <= MAX_HOSTS:
        for h in hosts:
            cmap[h["id"]] = h["id"]
        return hosts
    out = [h for h in hosts if h.get("pseudo")]
    bad = sorted((h for h in real if h["risk"]["level"] != "ok"), key=lambda h: -h["risk"]["score"])
    keep, rest = bad[:12], bad[12:]
    out += keep
    by_zone: dict[str, list[dict]] = defaultdict(list)
    for h in real:
        if h not in keep:
            by_zone[("more unhealthy nodes" if h in rest else h.get("zone") or (h.get("roles") or ["nodes"])[0])].append(h)
    for h in out:
        cmap[h["id"]] = h["id"]
    for zone, hs in sorted(by_zone.items()):
        gid = f"hostgrp:{zone}"
        total = lambda k: sum(h.get(k) or 0 for h in hs)      # noqa: E731
        cpu_a, mem_a = total("cpuAllocatable"), total("memAllocatable")
        ratio = lambda a, b: round(a / b, 3) if b else None    # noqa: E731
        level = _worst(h["risk"]["level"] for h in hs)
        out.append({
            "id": gid, "kind": "host", "subkind": "NodeGroup", "aggregate": "hosts", "name": f"{zone} · {len(hs)} nodes",
            "namespace": "", "ready": True, "readyStatus": "True", "readyMessage": "", "pressure": [], "taints": [],
            "zone": hs[0].get("zone", ""), "roles": [f"{len(hs)} nodes"], "unschedulable": False, "net": None,
            "agentStale": False, "kubelet": "", "os": "", "podCount": total("podCount"),
            "cpuAllocatable": cpu_a, "memAllocatable": mem_a, "cpuCapacity": total("cpuCapacity"),
            "memCapacity": total("memCapacity"), "cpuUsed": total("cpuUsed"), "memUsed": total("memUsed"),
            "cpuRequested": total("cpuRequested"), "memRequested": total("memRequested"), "memLimits": total("memLimits"),
            "podsWithoutMemLimit": total("podsWithoutMemLimit"), "cpuRatio": ratio(total("cpuUsed"), cpu_a),
            "cpuRequestRatio": ratio(total("cpuRequested"), cpu_a), "memRatio": ratio(total("memUsed"), mem_a),
            "memRequestRatio": ratio(total("memRequested"), mem_a), "memLimitRatio": ratio(total("memLimits"), mem_a),
            "members": [h["name"] for h in hs[:40]], "memberCount": len(hs),
            "risk": {"score": max(h["risk"]["score"] for h in hs), "level": level,
                     "reasons": [r for h in hs[:3] for r in h["risk"]["reasons"][:1]] if level != "ok" else []}})
        for h in hs:
            cmap[h["id"]] = gid
    return out


def _merge_edges(edges: list[dict], cmap: dict[str, str], shown: dict[str, dict]) -> tuple[list[dict], dict[str, str]]:
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in edges:
        s, t = cmap.get(e["source"]), cmap.get(e["target"])
        if s and t and s != t and s in shown and t in shown:
            buckets[(s, t)].append(e)
    out, emap = [], {}
    for (s, t), es in buckets.items():
        eid = f"{s}->{t}"
        for e in es:
            emap[e["id"]] = eid
        if len(es) == 1 and es[0]["source"] == s and es[0]["target"] == t:
            out.append(es[0])
            continue
        worst = max(es, key=lambda e: e["risk"]["score"])
        bad = sum(1 for e in es if e["risk"]["level"] != "ok")
        level = "ok" if not bad else "critical" if bad / len(es) >= 0.15 and worst["risk"]["level"] == "critical" else "warning"
        num = lambda sec, k: [e[sec][k] for e in es if e[sec].get(k) is not None]      # noqa: E731
        add = lambda sec, k: (sum(num(sec, k)) or None) if num(sec, k) else None       # noqa: E731
        top = lambda sec, k: max(num(sec, k)) if num(sec, k) else None                 # noqa: E731
        rps = num("traffic", "rps")
        err = [(e["health"].get("errorRate") or 0, e["traffic"].get("rps") or 0) for e in es]
        weight = sum(r for _e, r in err)
        traffic = {k: v for k, v in (("rps", add("traffic", "rps")), ("connections", add("traffic", "connections")),
                                     ("bytesPerSec", add("traffic", "bytesPerSec"))) if v is not None}
        traffic["level"] = max(e["traffic"].get("level") or 0 for e in es)
        if all(e["traffic"].get("idle") for e in es):
            traffic["idle"] = True
            traffic["lastActiveS"] = min(e["traffic"].get("lastActiveS") or 0 for e in es)
        latency = {k: v for k in ("p50Ms", "p95Ms", "rttMinMs", "rttP50Ms", "rttP95Ms", "connectP50Ms", "estimateMs")
                   if (v := top("latency", k)) is not None}
        latency["basis"] = worst["latency"].get("basis", "")
        health = {k: v for k in ("retrans", "pending", "sendQ", "recvQ") if (v := add("health", k))}
        if any(x for x, _r in err):
            health["errorRate"] = round(sum(x * r for x, r in err) / weight, 4) if weight else max(x for x, _r in err)
        names = lambda e: f"{e['source'].split('/')[-1]} → {e['target'].split('/')[-1]}"   # noqa: E731
        out.append({
            "id": eid, "source": s, "target": t, "type": worst["type"] if len({e["type"] for e in es}) == 1 else "calls",
            "evidence": sorted({x for e in es for x in e["evidence"]}), "via": [], "ports": [],
            "details": [f"{len(es)} calls between them" + (f", {len(rps)} with request metrics" if rps else "")],
            "traffic": traffic, "latency": latency, "health": health, "calls": len(es),
            "members": [e["id"] for e in sorted(es, key=lambda e: -e["risk"]["score"])[:12]],
            "troubled": bad,
            "risk": {"score": worst["risk"]["score"] if level == "critical" else min(0.5, worst["risk"]["score"]) if bad else 0.0,
                     "level": level,
                     "reasons": [{**r, "text": f"{bad} of {len(es)} calls in trouble, e.g. {names(worst)}: {r['text']}"}
                                 for r in worst["risk"]["reasons"][:2]] if bad else []}})
    # Between a few dozen units nearly everything talks to everything. Healthy, quiet bundles are kept
    # but marked minor: the map draws them only for the unit that is selected.
    limit = max(24, int(1.5 * len(shown)))
    ranked = sorted(out, key=lambda e: (-LEVEL_RANK[e["risk"]["level"]], -e.get("troubled", 0),
                                        -(e["traffic"].get("level") or 0)))
    for e in ranked[limit:]:
        if e["risk"]["level"] != "critical" and "calls" in e:
            e["minor"] = True
    return out, emap


# ------------------------------------------------------------------------------- server-side tables
def _device_cols(n: dict) -> dict:
    """Network Galaxy: the columns the device table sorts and shows."""
    d = n.get("device") or {}
    return {"ip": d.get("ip", ""), "mac": d.get("mac", ""), "vendor": d.get("vendor", ""), "type": d.get("typeLabel", ""),
            "present": bool(d.get("present")), "up": d.get("upBps", 0), "down": d.get("downBps", 0),
            "total": d.get("upBps", 0) + d.get("downBps", 0), "peers": d.get("peers", 0), "flows": d.get("flows", 0),
            "seen": d.get("lastSeenS", 0), "known": -(d.get("firstSeen") or 0), "blocked": d.get("dnsBlocked", 0),
            "forwards": len(d.get("forwards", [])), "score": d.get("ntopScore", 0)}


class Index:
    """Lookups over one generation of the full graph, built once and shared by every request."""

    def __init__(self, graph: dict):
        self.graph = graph
        self.nodes = {n["id"]: n for n in graph["nodes"]}
        self.edges = {e["id"]: e for e in graph["edges"]}
        self.inbound: dict[str, list[dict]] = defaultdict(list)
        self.outbound: dict[str, list[dict]] = defaultdict(list)
        for e in graph["edges"]:
            self.inbound[e["target"]].append(e)
            self.outbound[e["source"]].append(e)
        self.findings: dict[str, list[dict]] = defaultdict(list)
        for f in graph.get("findings", []):
            if f["subject"]:
                self.findings[f["subject"]].append(f)
        self._workloads: list[dict] | None = None
        self._calls: list[dict] | None = None

    def name(self, nid: str | None) -> str:
        if not nid:
            return "a dependency"
        n = self.nodes.get(nid)
        if n:
            return n["name"]
        e = self.edges.get(nid)
        return f"{self.name(e['source'])} → {self.name(e['target'])}" if e else nid.split("/")[-1]

    def workload_rows(self) -> list[dict]:
        if self._workloads is None:
            rows = []
            for n in self.graph["nodes"]:
                if n["kind"] != "workload":
                    continue
                inbound = [e for e in self.inbound[n["id"]] if e["type"] != "exposes"]
                outs = [e for e in self.outbound[n["id"]] if e["latency"].get("estimateMs") is not None]
                worst_out = max(outs, key=lambda e: e["latency"]["estimateMs"], default=None)
                top = next((f for f in self.findings[n["id"]] if f["severity"] != "info" and not f.get("effectOf")), None)
                risk = n["risk"]
                inherit = risk.get("impactLevel", "ok") != "ok" and risk["level"] == "ok"
                r, ro = n["replicas"], n.get("rollout")
                rows.append({
                    "id": n["id"], "name": n["name"], "ns": n["namespace"], "kind": n["subkind"], "platform": bool(n.get("platform")),
                    "risk": risk["score"] + (risk.get("impact") or 0) * 0.3,
                    "level": risk["level"] if risk["level"] != "ok" else "inherit" if inherit else "ok",
                    "readyN": r["ready"], "desired": r["desired"], "ready": r["ready"] / r["desired"] if r["desired"] else 1,
                    "rollout": {"state": ro["state"], "revision": ro["revision"], "ageSeconds": ro["ageSeconds"]} if ro else None,
                    "age": (ro or {}).get("ageSeconds") if (ro or {}).get("ageSeconds") is not None else 1e12,
                    "restarts": sum(p.get("recentRestarts") or 0 for p in n["pods"] if not p["terminal"]),
                    "cpu": (n.get("usage") or {}).get("cpuRatio") if (n.get("usage") or {}).get("cpuRatio") is not None else -1,
                    "mem": (n.get("usage") or {}).get("memRatio") if (n.get("usage") or {}).get("memRatio") is not None else -1,
                    "rps": sum(e["traffic"].get("rps") or 0 for e in inbound),
                    "inP95": max((e["latency"]["p95Ms"] for e in inbound if e["latency"].get("p95Ms") is not None), default=-1),
                    "inErr": max((e["health"].get("errorRate") or 0 for e in inbound), default=0),
                    "out": worst_out["latency"]["estimateMs"] if worst_out else -1,
                    "outName": self.name(worst_out["target"]) if worst_out else "",
                    **_device_cols(n),
                    "issue": top["title"] if top else f"Slowed by {self.name(risk.get('impactFrom'))}" if inherit else "",
                    "issueLvl": top["severity"] if top else risk.get("impactLevel", "ok") if inherit else "ok"})
            self._workloads = rows
        return self._workloads

    def call_rows(self) -> list[dict]:
        if self._calls is None:
            rows = []
            for e in self.graph["edges"]:
                if e["type"] == "exposes":
                    continue
                s, t = self.nodes.get(e["source"], {}), self.nodes.get(e["target"], {})
                f = next((x for x in self.findings[e["id"]] if x["severity"] != "info"), None)
                seen = "observed" in e["evidence"] or "mesh" in e["evidence"]
                note = (f["title"].split(": ", 1)[-1] if f["root"] else f"symptom of {f['causedByName']}") if f else \
                    "idle" if e["traffic"].get("idle") else "" if seen else "declared in config, no traffic seen"
                rows.append({
                    "id": e["id"], "risk": e["risk"]["score"], "level": e["risk"]["level"], "from": s.get("name", ""),
                    "to": t.get("name", ""), "fromNs": s.get("namespace") or s.get("kind", ""),
                    "toNs": t.get("namespace") or (t.get("scope") or "external" if t.get("kind") == "external" else t.get("kind", "")),
                    "ns": {x for x in (s.get("namespace"), t.get("namespace")) if x},
                    "platform": bool(t.get("platform")) and bool(s.get("platform") or not s.get("namespace")),
                    "external": t.get("kind") == "external",
                    "rps": e["traffic"].get("rps") if e["traffic"].get("rps") is not None else -1,
                    "conns": e["traffic"].get("connections") if e["traffic"].get("connections") is not None else -1,
                    "p50": e["latency"].get("p50Ms") if e["latency"].get("p50Ms") is not None else -1,
                    "p95": e["latency"].get("p95Ms") if e["latency"].get("p95Ms") is not None else -1,
                    "err": e["health"].get("errorRate") or 0,
                    "rtt": e["latency"].get("rttMinMs") if e["latency"].get("rttMinMs") is not None else -1,
                    "pending": e["health"].get("pending") or 0, "retrans": e["health"].get("retrans") or 0,
                    "bad": (e["health"].get("retrans") or 0) + (e["health"].get("pending") or 0) * 10,
                    "bps": round((e["traffic"].get("bytesPerSec") or 0) * 8), "bytes": e["traffic"].get("bytes") or 0,
                    "apps": ", ".join((e.get("net") or {}).get("apps", [])[:3]), "ports": ", ".join(e.get("ports", [])[:3]),
                    "type": e["type"],
                    "evidence": e["evidence"], "note": note})
            self._calls = rows
        return self._calls

    def page(self, rows: list[dict], *, namespaces, platform: bool, q: str, sort: str, desc: bool, offset: int,
             limit: int, only_problems: bool) -> dict:
        scope = set(namespaces or ())

        def in_scope(r: dict) -> bool:
            ns = r["ns"]
            if scope:
                return bool(ns & scope) if isinstance(ns, set) else ns in scope
            return platform or not r["platform"]

        q = (q or "").strip().lower()
        text = lambda r: f"{r.get('name', '')} {r.get('from', '')} {r.get('to', '')} {r.get('issue', '')} {r.get('note', '')} " \
                         f"{r.get('ip', '')} {r.get('mac', '')} {r.get('vendor', '')} {r.get('apps', '')} " \
                         f"{r['ns'] if isinstance(r['ns'], str) else ' '.join(r['ns'])}".lower()     # noqa: E731
        scoped = [r for r in rows if in_scope(r)]
        own = lambda r: r["level"] in ("warning", "critical")      # noqa: E731 - "inherit" is a victim, not a problem
        hits = [r for r in scoped if (not only_problems or own(r)) and (not q or q in text(r))]
        key = sort if hits and sort in hits[0] else "risk"
        if hits and isinstance(hits[0][key], str):
            hits.sort(key=lambda r: r[key].lower(), reverse=desc)
        else:
            hits.sort(key=lambda r: (r[key] if r[key] is not None else -1, r["risk"]), reverse=desc)
        rows_out = [{k: (sorted(v) if isinstance(v, set) else v) for k, v in r.items()} for r in hits[offset:offset + limit]]
        return {"rows": rows_out, "total": len(hits), "scope": len(scoped),
                "problems": sum(1 for r in scoped if own(r)), "offset": offset}

    def detail(self, nid: str) -> dict | None:
        """One node or call with everything the drawer needs, whether or not the page has it."""
        node, edge = self.nodes.get(nid), self.edges.get(nid)
        if not node and not edge:
            return None
        ids = {nid}
        edges = [edge] if edge else self.inbound[nid] + self.outbound[nid]
        neighbours = {x for e in edges for x in (e["source"], e["target"])} - ({nid} if node else set())

        def stub(x: str) -> dict:
            n = self.nodes[x]
            return {k: n[k] for k in ("id", "kind", "subkind", "name", "namespace", "platform", "scope") if k in n} | {
                "risk": {"level": n["risk"]["level"], "score": n["risk"]["score"], "reasons": [],
                         "impactLevel": n["risk"].get("impactLevel", "ok")},
                "pods": [], "hosts": [], "images": [], "services": [], "replicas": n.get("replicas") or {}, "stub": True}

        nodes = ([node] if node else []) + [stub(x) for x in neighbours if x in self.nodes]
        if node and node["kind"] == "workload":
            nodes += [self.nodes[h] for h in {p.get("hostId") for p in node["pods"]} if h in self.nodes]
        ids |= {e["id"] for e in edges}
        findings = [f for x in ids for f in self.findings.get(x, ())]
        cascades = [c for c in self.graph.get("cascades", []) if c["root"] == nid]
        for c in cascades:
            nodes += [stub(x) for x in c["impacted"] if x in self.nodes and x not in neighbours and x != nid]
        return {"id": nid, "nodes": nodes, "edges": edges, "findings": findings, "cascades": cascades}

    def search(self, q: str, limit: int = 15) -> list[dict]:
        q = q.strip().lower()
        if not q:
            return []
        hits: list[tuple[int, dict]] = []
        for n in self.graph["nodes"]:
            name = n["name"].lower()
            d = n.get("device") or {}
            if q in name or q in (n.get("namespace") or "").lower() or q in (d.get("mac") or "").lower() \
                    or q in (d.get("vendor") or "").lower() or (d.get("ip") or "").startswith(q) \
                    or any(a.startswith(q) for a in n.get("addresses") or ()):
                rank = 0 if name == q else 1 if name.startswith(q) else 2 if q in name else 3
                hits.append((rank, {"id": n["id"], "name": n["name"], "ns": n.get("namespace", ""),
                                    "kind": n.get("subkind") or n["kind"], "ip": d.get("ip", "")}))
        hits.sort(key=lambda h: (h[0], h[1]["name"]))
        return [h for _r, h in hits[:limit]]
