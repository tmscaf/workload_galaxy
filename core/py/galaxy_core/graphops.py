"""Graph operations every module shares: scope a graph to some groups, and turn a findings list into
the totals, patterns and capped list one response carries."""
from __future__ import annotations

import re
from collections import Counter, defaultdict

SEV_RANK = {"critical": 0, "warning": 1, "info": 2}


def _generic(text: str) -> str:
    """A finding's wording with the specifics taken out, so the same problem groups across workloads."""
    text = re.sub(r'"[^"]*"|\S*[0-9_/]\S*|\b[a-z0-9]+(?:-[a-z0-9]+)+\b', "", text)
    return re.sub(r"\s+([:,.])", r"\1", re.sub(r"\s{2,}", " ", text)).strip(" :,")


PATTERN_MIN = 4          # this many subjects with the same problem make it a fleet pattern
RESPONSE_FINDINGS = 300  # findings sent per response; the rest are reachable by narrowing the scope


def summarize(findings: list[dict], *, pattern: str | None = None, limit: int = RESPONSE_FINDINGS) -> dict:
    """What one response carries out of a (scoped) findings list: totals, fleet patterns, and the most
    severe findings that are not part of a pattern. With `pattern`, that pattern's members instead."""
    causes = [f for f in findings if f["root"] and not f.get("effectOf")]
    by_pattern: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for f in causes:
        by_pattern[(f["category"], f["pattern"])].append(f)
    patterns, member_subjects = [], set()
    for (category, key), fs in by_pattern.items():
        subjects = {f["subject"] or f["id"] for f in fs}
        if len(subjects) < PATTERN_MIN:
            continue
        worst = fs[0]                                   # findings arrive sorted: most severe first
        pkey = f"{category}|{key}"
        if worst["severity"] != "info":
            member_subjects |= {f["subject"] for f in fs if f["subject"]}
        groups = Counter((f["namespace"] or "cluster").split("-")[0] for f in fs)
        patterns.append({
            "key": pkey, "category": category, "severity": worst["severity"],
            "label": worst.get("patternLabel") or _generic(worst["title"]) or worst["title"],
            "count": len(subjects), "projects": len({f["namespace"] for f in fs if f["namespace"]}),
            "units": [{"name": g, "count": c} for g, c in groups.most_common(4)],
            "userFacing": sum(1 for f in fs if f["userFacing"]), "cause": worst["cause"], "fix": worst["fix"],
            "kind": worst["subjectKind"] or "project", "sizing": any(f.get("options") for f in fs),
            "examples": [{"id": f["id"], "subject": f["subject"], "subjectName": f["subjectName"],
                          "namespace": f["namespace"], "title": f["title"]} for f in fs[:5]]})
        for f in fs:
            f["_pattern"] = pkey
    patterns.sort(key=lambda p: (SEV_RANK[p["severity"]], -p["count"]))
    real = [f for f in causes if f["severity"] != "info"]
    counts = {k: sum(1 for f in findings if f["severity"] == k) for k in SEV_RANK}
    summary = {
        "findings": len(findings), **counts,
        "problems": len({f["subject"] or f["id"] for f in real}),
        "problemsCritical": len({f["subject"] or f["id"] for f in real if f["severity"] == "critical"}),
        "projects": len({f["namespace"] for f in real if f["namespace"]}),
        "userFacing": len({f["subject"] or f["id"] for f in real if f["userFacing"]})}
    if pattern:
        subjects = {f["subject"] for f in causes if f.get("_pattern") == pattern}
        chosen = [f for f in findings if f.get("_pattern") == pattern
                  or (f["subject"] in subjects and f["subject"]) or f.get("causedBy") in subjects]
    else:
        # Members of a fleet pattern (and the symptoms they cause) are represented by the pattern.
        fleet = {p["key"] for p in patterns}
        chosen = [f for f in findings if f.get("_pattern") not in fleet
                  and not (f["subject"] in member_subjects and f["severity"] != "info" and f.get("effectOf"))
                  and f.get("causedBy") not in member_subjects]
    out = [{k: v for k, v in f.items() if k != "_pattern"} for f in chosen[:limit]]
    for f in findings:
        f.pop("_pattern", None)
    return {"findings": out, "patterns": patterns, "summary": summary, "findingsShown": len(out),
            "findingsMatching": len(chosen)}


def filter_graph(graph: dict, namespaces: list[str] | None, include_platform: bool, pattern: str | None = None) -> dict:
    """Restrict to some namespaces, keeping one-hop neighbours outside them as 'ghost' nodes."""
    nodes = {n["id"]: n for n in graph["nodes"]}

    def in_scope(n: dict) -> bool:
        ns = n.get("namespace")
        if not ns:
            return False
        if namespaces:
            return ns in namespaces
        return include_platform or not n.get("platform")

    # Only namespaced nodes in scope pull in neighbours; clients/externals come along when linked.
    core = {nid for nid, n in nodes.items() if in_scope(n)}
    keep = set(core)
    ghosts = set()
    edges = []
    for e in graph["edges"]:
        s, t = e["source"], e["target"]
        if s not in core and t not in core:
            continue
        for other in (s, t):
            if other in core:
                continue
            if nodes[other]["kind"] in ("workload", "entry", "service"):
                ghosts.add(other)
            keep.add(other)
        edges.append(e)
    # Show the outside-world path into ghost entries too (clients -> controller -> route).
    for e in graph["edges"]:
        if e["target"] in ghosts and nodes[e["source"]]["kind"] == "clients" and e not in edges:
            keep.add(e["source"])
            edges.append(e)
    # What a kept rim node hangs off in turn comes along too (a network's uplink, a service's upstream).
    have = {e["id"] for e in edges}
    grew = True
    while grew:
        grew = False
        for e in graph["edges"]:
            s_, t_ = e["source"], e["target"]
            if e["id"] not in have and s_ in keep and s_ not in core and nodes[s_]["kind"] == "external" \
                    and nodes[t_]["kind"] == "external":
                keep.add(t_)
                edges.append(e)
                have.add(e["id"])
                grew = True
    used_hosts = {p["hostId"] for nid in keep if nodes[nid]["kind"] == "workload" for p in nodes[nid]["pods"]}
    out_nodes = []
    for nid in keep:
        n = dict(nodes[nid])
        n["ghost"] = nid in ghosts
        out_nodes.append(n)
    # A host comes along when something in view sits on it, or when the module pins it (the one
    # hypervisor everything runs on stays on the map even with every VM switched off).
    out_nodes += [dict(n, ghost=False) for n in graph["nodes"]
                  if n["kind"] == "host" and (n["id"] in used_hosts or n.get("pinned"))]
    keep |= {n["id"] for n in out_nodes if n["kind"] == "host"}
    out = {k: v for k, v in graph.items() if k not in ("nodes", "edges")}
    out["nodes"] = out_nodes
    out["edges"] = edges
    keep_ids = keep | {e["id"] for e in edges}
    out["hotspots"] = [h for h in graph.get("hotspots", []) if h["id"] in keep_ids]
    out["paths"] = [p for p in graph.get("paths", []) if all(x in keep_ids for x in p["nodes"])]
    out["cascades"] = []
    for c in graph.get("cascades", []):
        impacted = [x for x in c["impacted"] if x in keep]
        if c["root"] in keep_ids and impacted:
            dominant = [x for x in c.get("dominant", []) if x in keep]
            out["cascades"].append(dict(c, impacted=impacted, dominant=dominant, count=len(impacted)))

    def ns_in_scope(ns: str, platform: bool) -> bool:
        if namespaces:
            return ns in namespaces
        return include_platform or not platform

    # Findings: everything in the selected projects, plus cluster-level ones (nodes, platform), which
    # explain project symptoms whatever the filter.
    scoped = [f for f in graph.get("findings", [])
              if (f["namespace"] and ns_in_scope(f["namespace"], f["platform"]))
              or (not f["namespace"] and (f["category"] in ("node", "platform") or f["subject"] in keep_ids
                                          or not f["subject"]))]      # network-wide: shown whatever the filter
    out.update(summarize(scoped, pattern=pattern))
    out["_scoped"] = scoped          # for scale.collapse(); never serialized (see main._view)
    out["changes"] = [c for c in graph.get("changes", [])
                      if (c["namespace"] and ns_in_scope(c["namespace"], False))
                      or (not c["namespace"])]
    return out
