# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Diagnostics: turn cluster state into findings a person can act on.

The topology and risk modules answer "what is slow or down, and who does it
hurt". This module answers "why, and what do I do": it reads the objects that
explain a failure (events, rollout status, container exit codes, claims,
quotas, policies, route status, platform operators) and emits *findings*:

    {id, severity, category, title, subject, namespace, pods, detail, cause, fix,
     commands, evidence, sinceS, root, causedBy, blast, userFacing}

Two passes around risk scoring:
  run()     before scoring: specific detectors. A finding may carry a weight,
            which is added to its subject's risk so the maps show it too.
  finish()  after scoring: every remaining risk signal becomes a finding with
            a cause and next step; symptoms are linked to their root cause;
            the change timeline and platform summary are assembled.

Secrets are never read. Events and messages are quoted as the API serves them.
"""
from __future__ import annotations

import difflib
import re
from collections import Counter, defaultdict

from . import advice as A
from .risk import bad_reason
from .sizing import Sizer
from .util import controller_ref, labels_of, meta, name_of, ns_of, parse_cpu, parse_mem, parse_ts, now_utc

IMAGE_REASONS = {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull", "ImageInspectError"}
CONFIG_REASONS = {"CreateContainerConfigError", "CreateContainerError", "RunContainerError", "StartError"}
MAX_CHANGES = 80
SEV_RANK = {"critical": 0, "warning": 1, "info": 2}


def dur(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    s = int(seconds)
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{round(s / 60)}m"
    if s < 172800:
        return f"{round(s / 3600)}h"
    return f"{round(s / 86400)}d"


def sel_match(selector: dict | None, labels: dict) -> bool:
    """LabelSelector semantics: {} matches everything."""
    selector = selector or {}
    for k, v in (selector.get("matchLabels") or {}).items():
        if labels.get(k) != v:
            return False
    for ex in selector.get("matchExpressions") or []:
        key, op, vals = ex.get("key"), ex.get("operator"), ex.get("values") or []
        if op == "In" and labels.get(key) not in vals:
            return False
        if op == "NotIn" and labels.get(key) in vals:
            return False
        if op == "Exists" and key not in labels:
            return False
        if op == "DoesNotExist" and key in labels:
            return False
    return True


def _short_image(image: str) -> str:
    return image.split("@", 1)[0].rsplit("/", 1)[-1]


def _qty(resource: str, value) -> float | None:
    if value is None:
        return None
    if "cpu" in resource:
        return parse_cpu(value)
    if "memory" in resource or "storage" in resource:
        return parse_mem(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return parse_mem(value)


class Diagnoser:
    def __init__(self, builder, edges: list[dict], *, event_window_seconds: float = 3600):
        self.b = builder
        self.snap = builder.snap
        self.edges = edges
        self.edge_by_id = {e["id"]: e for e in edges}
        self.window = event_window_seconds
        self.now = now_utc()
        self.openshift = bool(self.snap.get("openshift"))
        self.cli = "oc" if self.openshift else "kubectl"
        self.findings: dict[str, dict] = {}
        self.covered: set[tuple[str, str]] = set()      # (subject, risk code) already explained by a finding
        self.changes: list[dict] = []
        self.platform: dict = {}
        self.quota_ratio: dict[str, float] = {}
        # (finding id, kind, facts): resource-constraint findings the sizing advisor adds options to.
        self.sizing_hints: list[tuple[str, str, dict]] = []
        self.sizer = Sizer(self, getattr(builder, "usage_history", None),
                           min_history=getattr(builder, "sizing_min_history", 3600.0))

    # ------------------------------------------------------------------ helpers
    def _age(self, ts: str | None) -> int | None:
        t = parse_ts(ts)
        return int((self.now - t).total_seconds()) if t else None

    def workloads(self):
        return [(nid, n) for nid, n in self.b.nodes.items() if n["kind"] == "workload"]

    def ref(self, n: dict) -> str:
        return f"{A.REF.get(n.get('subkind', ''), n.get('subkind', '').lower())}/{n['name']}"

    def cmd(self, template: str, **kw) -> str:
        return template.format(cli=self.cli, **kw)

    def add(self, category: str, key: str, title: str, *, subject: str = "", namespace: str = "",
            severity: str | None = None, weight: float | None = None, detail: str = "", cause: str = "",
            fix: str = "", commands: list[str] | tuple = (), pods: list[str] | tuple = (),
            evidence: list[str] | tuple = (), since_s: int | None = None, covers: tuple = (),
            risk_category: str | None = None, pattern: str | None = None, label: str | None = None) -> dict | None:
        node = self.b.nodes.get(subject)
        edge = self.edge_by_id.get(subject)
        if edge:
            s, t = self.b.nodes.get(edge["source"], {}), self.b.nodes.get(edge["target"], {})
            ns = namespace or t.get("namespace") or s.get("namespace", "")
            name, kind = f"{s.get('name', '?')} → {t.get('name', '?')}", "call"
            platform = bool(t.get("platform")) and bool(s.get("platform") or not s.get("namespace"))
        else:
            ns = namespace or (node or {}).get("namespace", "")
            name = (node or {}).get("name", "")
            kind = (node or {}).get("kind", "")
            platform = bool((node or {}).get("platform")) if node else bool(ns and _platform_ns(ns))
        if ns and ns in self.b.hidden:
            return None
        sev = severity or ("critical" if (weight or 0) >= 0.6 else "warning" if (weight or 0) >= 0.25 else "info")
        fid = f"{category}:{key}"
        for code in covers:
            self.covered.add((subject, code))
        if fid in self.findings:
            return self.findings[fid]
        f = {"id": fid, "severity": sev, "category": category, "title": title, "subject": subject if (node or edge) else "",
             "subjectKind": kind, "subjectName": name, "namespace": ns, "platform": platform, "pods": list(pods)[:6],
             "podCount": len(pods), "detail": detail, "cause": cause, "fix": fix, "commands": list(commands)[:4],
             "evidence": [e[:500] for e in evidence if e][:4], "sinceS": since_s, "root": True, "causedBy": None,
             "causedByName": None, "blast": 0, "userFacing": False, "weight": round(weight or 0, 3),
             # The kind of problem, whoever has it: lets one cause across many workloads be shown once.
             "pattern": pattern or key.split(":", 1)[0], "patternLabel": label or A.PATTERN_LABELS.get(pattern or key.split(":", 1)[0], "")}
        self.findings[fid] = f
        if weight and (node or edge):
            (node or edge).setdefault("diag", []).append(
                {"weight": weight, "text": title, "category": risk_category or category})
        return f

    # ------------------------------------------------------------------ pass 1
    def run(self) -> None:
        self._index_events()
        self._listen_ports()
        self._rollouts()
        self._admission()
        self._pods()
        self._storage()
        self._quotas()
        self._services()
        self._entries()
        self._network_policies()
        self._batch()
        self._hpa()
        self._certificates()
        self._openshift_platform()
        self._hygiene()
        self.sizer._guard(self.sizer.run)

    # ---- events
    def _index_events(self) -> None:
        self.obj_events: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for e in self.snap.get("events") or []:
            if e.get("type") == "Normal":
                continue
            series = e.get("series") or {}
            age = self._age(e.get("lastTimestamp") or series.get("lastObservedTime") or e.get("eventTime")
                            or meta(e).get("creationTimestamp"))
            if age is not None and age > self.window:
                continue
            io = e.get("involvedObject") or e.get("regarding") or {}
            ev = {"reason": e.get("reason", ""), "message": (e.get("message") or e.get("note") or "")[:500],
                  "count": int(e.get("count") or series.get("count") or 1), "ageS": age,
                  "kind": io.get("kind", ""), "name": io.get("name", ""),
                  "namespace": io.get("namespace") or ns_of(e)}
            self.obj_events[(ev["kind"], ev["namespace"], ev["name"])].append(ev)
        for evs in self.obj_events.values():
            evs.sort(key=lambda x: x["ageS"] if x["ageS"] is not None else 1e9)

        hpa_target = {}
        for h in self.snap.get("hpas") or []:
            t = (h.get("spec") or {}).get("scaleTargetRef") or {}
            hpa_target[(ns_of(h), name_of(h))] = f"wl:{ns_of(h)}/{t.get('kind')}/{t.get('name')}"
        self.claim_users: dict[tuple[str, str], set[str]] = defaultdict(set)
        for wid, n in self.workloads():
            for p in n["pods"]:
                for claim in p.get("claims") or []:
                    self.claim_users[(n["namespace"], claim)].add(wid)

        def owner_wid(kind: str, ns: str, name: str) -> list[str]:
            if kind == "Pod":
                wid = self.b.pod_wl.get(f"{ns}/{name}")
                if wid:
                    return [wid]
                base = name.rsplit("-", 1)[0]          # a pod that is already gone: find its controller by name
                return owner_wid("ReplicaSet", ns, base) or \
                    [w for w in (f"wl:{ns}/StatefulSet/{base}", f"wl:{ns}/DaemonSet/{base}") if w in self.b.nodes]
            if kind == "ReplicaSet":
                rs = self.b.rs.get((ns, name))
                o = controller_ref(rs) if rs else None
                dep = o["name"] if o and o.get("kind") == "Deployment" else name.rsplit("-", 1)[0]
                return [w for w in (f"wl:{ns}/Deployment/{dep}", f"wl:{ns}/ReplicaSet/{name}") if w in self.b.nodes][:1]
            if kind == "ReplicationController":
                rc = self.b.rc.get((ns, name))
                dc = (meta(rc).get("annotations") or {}).get("openshift.io/deployment-config.name") if rc else None
                return [w for w in (f"wl:{ns}/DeploymentConfig/{dc or name.rsplit('-', 1)[0]}",) if w in self.b.nodes]
            if kind == "Job":
                o = controller_ref(self.b.jobs.get((ns, name)))
                wid = f"wl:{ns}/CronJob/{o['name']}" if o and o.get("kind") == "CronJob" else f"wl:{ns}/Job/{name}"
                return [wid] if wid in self.b.nodes else []
            if kind == "HorizontalPodAutoscaler":
                wid = hpa_target.get((ns, name))
                return [wid] if wid in self.b.nodes else []
            if kind == "PersistentVolumeClaim":
                return sorted(self.claim_users.get((ns, name), ()))
            wid = f"wl:{ns}/{kind}/{name}"
            return [wid] if wid in self.b.nodes else []

        self.wl_events: dict[str, list[dict]] = defaultdict(list)
        self.host_events: dict[str, list[dict]] = defaultdict(list)
        for (kind, ns, name), evs in self.obj_events.items():
            if kind == "Node":
                self.host_events[name].extend(evs)
                continue
            for wid in owner_wid(kind, ns, name):
                if wid in self.b.nodes:            # not a workload in a hidden namespace (e.g. this tool's own)
                    self.wl_events[wid].extend(evs)
        for wid, evs in self.wl_events.items():
            evs.sort(key=lambda x: x["ageS"] if x["ageS"] is not None else 1e9)
            seen, out = set(), []
            for ev in evs:
                key = (ev["reason"], ev["message"][:90])
                if key not in seen:
                    seen.add(key)
                    out.append(ev)
            self.b.nodes[wid]["events"] = out[:12]
        for name, evs in self.host_events.items():
            host = self.b.nodes.get(f"host:{name}")
            if host:
                evs.sort(key=lambda x: x["ageS"] if x["ageS"] is not None else 1e9)
                host["events"] = evs[:10]

    def _ev(self, kind: str, ns: str, name: str, *reasons: str, contains: str | None = None) -> dict | None:
        for ev in self.obj_events.get((kind, ns, name), ()):
            if (not reasons or ev["reason"] in reasons) and (not contains or contains.lower() in ev["message"].lower()):
                return ev
        return None

    def _listen_ports(self) -> None:
        """TCP ports each pod really listens on (from the node agent), for wiring checks."""
        self.listen: dict[str, set[int]] = {}
        for r in self.b.reports:
            for netns in r.get("netns") or []:
                key = self.b.pod_uid.get(netns.get("podUid", ""))
                if key is not None:
                    self.listen[key] = {int(x["port"]) for x in netns.get("listen") or [] if x.get("port")}

    # ---- rollouts
    def _rollouts(self) -> None:
        self.replica_failure: dict[str, str] = {}
        by_owner: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for (ns, _name), rs in self.b.rs.items():
            o = controller_ref(rs)
            if o and o.get("kind") == "Deployment":
                by_owner[(ns, o["name"])].append(rs)

        def rev(rs: dict) -> int:
            try:
                return int((meta(rs).get("annotations") or {}).get("deployment.kubernetes.io/revision") or 0)
            except ValueError:
                return 0

        for dep in self.snap.get("deployments") or []:
            ns, name = ns_of(dep), name_of(dep)
            wid = f"wl:{ns}/Deployment/{name}"
            n = self.b.nodes.get(wid)
            if not n:
                continue
            spec, status = dep.get("spec") or {}, dep.get("status") or {}
            conds = {c.get("type"): c for c in status.get("conditions") or []}
            if (conds.get("ReplicaFailure") or {}).get("status") == "True":
                self.replica_failure[wid] = conds["ReplicaFailure"].get("message", "")
            rss = sorted(by_owner.get((ns, name), []), key=lambda r: (rev(r), meta(r).get("creationTimestamp") or ""))
            if not rss:
                continue
            new, prev = rss[-1], (rss[-2] if len(rss) > 1 else None)
            new_name = name_of(new)
            desired = n["replicas"]["desired"]
            live = [p for p in n["pods"] if not p["terminal"]]
            new_pods = [p for p in live if p.get("owner") == new_name]
            old_pods = [p for p in live if p.get("owner") != new_name and p.get("ownerKind") == "ReplicaSet"]
            new_ready = sum(1 for p in new_pods if p["ready"])
            age = self._age(meta(new).get("creationTimestamp"))
            prog = conds.get("Progressing") or {}
            failed = prog.get("status") == "False" or prog.get("reason") == "ProgressDeadlineExceeded"
            blocked = Counter(p["reason"] for p in new_pods
                              if not p["ready"] and (bad_reason(p["reason"]) or (p["phase"] == "Pending" and not p["node"])))
            in_flight = bool(old_pods) or (prev is not None and age is not None and age < 600 and new_ready < desired)
            stuck = bool(old_pods) and (bool(blocked) or (age is not None and age > 600)) and (age or 0) > 60
            state = "failed" if failed else "stuck" if stuck else "paused" if spec.get("paused") else \
                "progressing" if in_flight else "complete"
            changes = _template_diff(prev, new) if prev else []
            n["rollout"] = {"state": state, "revision": rev(new) or None, "ageSeconds": age, "updated": len(new_pods),
                            "updatedReady": new_ready, "desired": desired, "old": len(old_pods), "changes": changes[:8],
                            "strategy": (spec.get("strategy") or {}).get("type", "RollingUpdate"),
                            "message": prog.get("message", "") if failed else ""}
            if age is not None and age < 86400 and (prev is not None or age < 3600):
                self.changes.append({
                    "ageS": age, "kind": "rollout", "subject": wid, "namespace": ns, "level": {
                        "failed": "critical", "stuck": "critical", "progressing": "info"}.get(state, "ok"),
                    "title": f"{name} " + (f"rolled out revision {rev(new)}" if prev is not None and rev(new)
                                           else "rolled out a new version" if prev is not None else "deployed"),
                    "detail": "; ".join(changes[:4]) + (f" · {state}" if state != "complete" else "")})
            if state not in ("failed", "stuck"):
                continue
            serving = n["replicas"]["ready"]
            why = ", ".join(f"{c} new pod{'s' if c > 1 else ''} {r or 'not ready'}" for r, c in blocked.items()) \
                or prog.get("message", "") or "The new pods never became ready."
            self.add(
                "rollout", wid, f"Rollout {state}: {new_ready}/{desired} new pods ready after {dur(age)}",
                pattern="rollout", label="rollout stuck or failed",
                subject=wid, weight=0.65 if serving else 0.85, since_s=age,
                detail=(f"Revision {rev(new)}. " if rev(new) else "") + (f"Changed: {'; '.join(changes[:5])}." if changes else ""),
                cause=why + ("" if not blocked else " — see the finding for those pods."),
                fix=(f"The previous version still serves traffic ({serving} ready pod{'s' if serving != 1 else ''}), so "
                     "users are unaffected for now. " if serving else "Nothing is serving. ")
                + "Fix what blocks the new pods, or roll back to the last working revision.",
                commands=[self.cmd("{cli} -n {ns} rollout status deploy/{name}", ns=ns, name=name),
                          self.cmd("{cli} -n {ns} rollout undo deploy/{name}", ns=ns, name=name),
                          self.cmd("{cli} -n {ns} rollout history deploy/{name}", ns=ns, name=name)],
                covers=("replicas",))

        # StatefulSets / DaemonSets: revision history lives in ControllerRevisions.
        revs: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for cr in self.snap.get("controllerrevisions") or []:
            o = controller_ref(cr)
            if o:
                revs[(ns_of(cr), o.get("kind", ""), o.get("name", ""))].append(cr)
        for key, kind in (("statefulsets", "StatefulSet"), ("daemonsets", "DaemonSet")):
            for obj in self.snap.get(key) or []:
                ns, name = ns_of(obj), name_of(obj)
                wid = f"wl:{ns}/{kind}/{name}"
                n = self.b.nodes.get(wid)
                st = obj.get("status") or {}
                if not n:
                    continue
                history = sorted(revs.get((ns, kind, name), []), key=lambda c: int(c.get("revision") or 0))
                age = self._age(meta(history[-1]).get("creationTimestamp")) if history else None
                desired = n["replicas"]["desired"]
                if kind == "StatefulSet":
                    rolling = bool(st.get("updateRevision")) and st.get("currentRevision") != st.get("updateRevision")
                    updated = int(st.get("updatedReplicas") or 0)
                else:
                    updated = int(st.get("updatedNumberScheduled") or 0) if "updatedNumberScheduled" in st else desired
                    rolling = updated < desired
                if not history and not rolling:
                    continue
                unready = [p for p in n["pods"] if not p["terminal"] and not p["ready"]]
                stuck = rolling and bool(unready) and (age is None or age > 600)
                state = "stuck" if stuck else "progressing" if rolling else "complete"
                n["rollout"] = {"state": state, "revision": int(history[-1].get("revision") or 0) if history else None,
                                "ageSeconds": age, "updated": updated, "updatedReady": None, "desired": desired,
                                "old": max(0, desired - updated) if rolling else 0, "changes": [],
                                "strategy": (obj.get("spec") or {}).get("updateStrategy", {}).get("type", "RollingUpdate"),
                                "message": ""}
                if history and len(history) > 1 and age is not None and age < 86400:
                    self.changes.append({"ageS": age, "kind": "rollout", "subject": wid, "namespace": ns,
                                         "level": "critical" if stuck else "info" if rolling else "ok",
                                         "title": f"{name} updated to revision {history[-1].get('revision')}",
                                         "detail": state if state != "complete" else ""})
                if stuck:
                    self.add("rollout", wid, f"Rollout stuck: {updated}/{desired} pods updated, {len(unready)} not ready",
                             subject=wid, weight=0.65, since_s=age,
                             cause=("A StatefulSet updates one pod at a time and waits for it to become ready; "
                                    f"{unready[0]['name']} isn't." if kind == "StatefulSet"
                                    else "Updated pods on some nodes aren't becoming ready."),
                             fix="Fix the pod that isn't ready (see its finding), or roll back.",
                             commands=[self.cmd("{cli} -n {ns} rollout status {ref}", ns=ns, ref=self.ref(n)),
                                       self.cmd("{cli} -n {ns} rollout undo {ref}", ns=ns, ref=self.ref(n))],
                             covers=("replicas",))

        # OpenShift DeploymentConfigs: each rollout is a ReplicationController with a phase annotation.
        latest: dict[tuple[str, str], dict] = {}
        for (ns, _n), rc in self.b.rc.items():
            ann = meta(rc).get("annotations") or {}
            dc = ann.get("openshift.io/deployment-config.name")
            if not dc:
                continue
            try:
                ver = int(ann.get("openshift.io/deployment-config.latest-version") or 0)
            except ValueError:
                ver = 0
            cur = latest.get((ns, dc))
            if not cur or ver > cur["_ver"]:
                latest[(ns, dc)] = {"_ver": ver, "rc": rc, "ann": ann}
        for (ns, dc), info in latest.items():
            wid = f"wl:{ns}/DeploymentConfig/{dc}"
            n = self.b.nodes.get(wid)
            if not n:
                continue
            phase = info["ann"].get("openshift.io/deployment.phase", "")
            age = self._age(meta(info["rc"]).get("creationTimestamp"))
            state = {"Failed": "failed", "Complete": "complete", "Running": "progressing", "Pending": "progressing",
                     "New": "progressing"}.get(phase, "complete")
            n["rollout"] = {"state": state, "revision": info["_ver"] or None, "ageSeconds": age, "updated": None,
                            "updatedReady": None, "desired": n["replicas"]["desired"], "old": 0, "changes": [],
                            "strategy": "DeploymentConfig",
                            "message": info["ann"].get("openshift.io/deployment.status-reason", "")}
            if age is not None and age < 86400 and info["_ver"] > 1:
                self.changes.append({"ageS": age, "kind": "rollout", "subject": wid, "namespace": ns,
                                     "level": "critical" if state == "failed" else "ok",
                                     "title": f"{dc} deployment #{info['_ver']} {phase.lower() or 'started'}", "detail": ""})
            if state == "failed":
                rc_name = name_of(info["rc"])
                self.add("rollout", wid, f"Deployment #{info['_ver']} failed", subject=wid, since_s=age,
                         weight=0.65 if n["replicas"]["ready"] else 0.85,
                         cause=n["rollout"]["message"] or "The deployer pod gave up: the new pods did not become ready "
                                                          "within the timeout.",
                         fix="Read the deployer pod's log, fix the new version, then roll out again. The previous "
                             "deployment was scaled back up.",
                         commands=[f"oc -n {ns} logs {rc_name}-deploy", f"oc -n {ns} rollout latest dc/{dc}",
                                   f"oc -n {ns} rollout history dc/{dc}"], covers=("replicas",))

    # ---- controllers that can't create pods
    def _admission(self) -> None:
        for wid, n in self.workloads():
            ev = next((e for e in self.wl_events.get(wid, ()) if e["reason"] in ("FailedCreate", "FailedCreatePod")), None)
            msg = self.replica_failure.get(wid) or (ev or {}).get("message", "")
            if not msg:
                continue
            category, what, cause, fix = A.admission_advice(msg, self.openshift)
            ns, ready = n["namespace"], n["replicas"]["ready"]
            commands = [self.cmd("{cli} -n {ns} describe {ref}", ns=ns, ref=self.ref(n))]
            if category == "quota":
                commands.insert(0, self.cmd("{cli} -n {ns} describe quota", ns=ns))
            elif "security" in what and self.openshift:
                commands.insert(0, f"oc -n {ns} get {self.ref(n)} -o yaml | oc adm policy scc-subject-review -f -")
            f = self.add(category, f"create:{wid}", f"Can't create pods: {what}", subject=wid,
                         pattern=f"create:{what}", label=f"can't create pods: {what}",
                         weight=0.6 if ready else 0.85, cause=cause, fix=fix, commands=commands, evidence=[msg],
                         since_s=(ev or {}).get("ageS"), covers=("replicas",), risk_category="availability")
            if f and (category == "quota" or "LimitRange" in what):
                self.sizing_hints.append((f["id"], "quota_create" if category == "quota" else "limitrange",
                                          {"wid": wid, "message": msg}))

    # ---- pods
    def _pods(self) -> None:
        for wid, n in self.workloads():
            groups: dict[tuple[str, str], dict] = {}
            for p in n["pods"]:
                if p["terminal"]:
                    continue
                host = self.b.nodes.get(p.get("hostId", ""))
                if host and not host.get("pseudo") and not host.get("ready"):
                    continue                              # the dead node is the story, not the pod
                for kind, sig, data in self._pod_problems(n, p):
                    g = groups.setdefault((kind, sig), {"pods": [], "data": data})
                    g["pods"].append(p["name"])
            for (kind, sig), g in groups.items():
                d, pods = g["data"], g["pods"]
                count = len(pods)
                noun = f"{count} pod{'s' if count > 1 else ''}"
                klass = d.get("klass") or _generic(d["what"])
                f = self.add(d["category"], f"{kind}:{wid}:{sig}"[:200], f"{noun} {d['what']}", subject=wid, pods=pods,
                             pattern=f"{kind}:{klass}", label=klass,
                             severity=d.get("severity", "critical"), cause=d["cause"], fix=d["fix"],
                             commands=[c.replace("{pod}", pods[0]) for c in d.get("commands", [])],
                             evidence=d.get("evidence", []), since_s=d.get("since"), detail=d.get("detail", ""),
                             covers=d.get("covers", ()))
                if f and d.get("sizing"):
                    self.sizing_hints.append((f["id"], d["sizing"][0], {**d["sizing"][1], "wid": wid, "count": count}))

    def _pod_problems(self, n: dict, p: dict) -> list[tuple[str, str, dict]]:
        ns, pod = n["namespace"], p["name"]
        cs = p.get("containerInfo") or []
        describe = self.cmd("{cli} -n {ns} describe pod {{pod}}", ns=ns)

        def logs(container: str, previous: bool) -> str:
            return self.cmd("{cli} -n {ns} logs {{pod}} -c {c}{prev} --tail=50", ns=ns, c=container,
                            prev=" --previous" if previous else "")

        # Not scheduled.
        if p["phase"] == "Pending" and not p["node"]:
            what, cause, fix = A.sched_advice(p["message"])
            req = []
            if p.get("cpuRequest"):
                req.append(f"cpu {p['cpuRequest']:g}")
            if p.get("memRequest"):
                req.append(f"memory {p['memRequest'] / 1024 ** 2:.0f}Mi")
            return [("sched", what, {
                "category": "scheduling", "what": f"can't be scheduled: {what}", "cause": cause, "fix": fix,
                "klass": f"can't be scheduled: {what}",
                "detail": f"Each pod requests {', '.join(req)}." if req else "", "evidence": [p["message"]],
                "since": p.get("ageSeconds"), "covers": ("unschedulable", "pod:Unschedulable", "replicas"),
                "sizing": ("sched", {"pod": p, "message": p["message"]}),
                "commands": [describe, self.cmd("{cli} describe nodes | grep -A6 'Allocated resources'")]})]

        # Image can't be pulled.
        c = next((c for c in cs if c["state"] == "waiting" and c["reason"] in IMAGE_REASONS), None)
        if c:
            ev = self._ev("Pod", ns, pod, "Failed", "ErrImagePull", "InspectFailed", contains="image") \
                or self._ev("Pod", ns, pod, "Failed")
            msg = (ev or {}).get("message") or c["message"]
            what, cause, fix = A.image_advice(f"{c['reason']} {msg} {c['message']}")
            return [("image", c["image"], {
                "category": "image", "what": f"can't pull {_short_image(c['image'])}: {what}", "cause": cause, "fix": fix,
                "klass": f"can't pull their image: {what}",
                "detail": f"Image: {c['image']}", "evidence": [msg], "since": p.get("ageSeconds"),
                "covers": tuple(f"pod:{r}" for r in IMAGE_REASONS) + ("replicas",),
                "commands": [describe, self.cmd("{cli} -n {ns} get pod {{pod}} -o jsonpath='{{.spec.imagePullSecrets}}'",
                                                ns=ns)]})]

        # Container can't be created from its spec.
        c = next((c for c in cs if c["state"] == "waiting" and c["reason"] in CONFIG_REASONS), None)
        if c:
            what, cause, fix = A.config_advice(c["message"])
            near = self._near_config(ns, c["message"])
            return [("config", what, {
                "category": "config", "what": f"can't start: {what}", "cause": cause + near, "fix": fix,
                "evidence": [c["message"]], "since": p.get("ageSeconds"),
                "covers": tuple(f"pod:{r}" for r in CONFIG_REASONS) + ("replicas",),
                "commands": [describe, self.cmd("{cli} -n {ns} get configmaps,secrets", ns=ns)]})]

        out: list[tuple[str, str, dict]] = []
        # Crashing (main or init container).
        crash = next((c for c in cs if (c["state"] == "waiting" and c["reason"] == "CrashLoopBackOff")
                      or (c["state"] == "terminated" and c.get("exitCode") not in (0, None) and (c["init"] or p["phase"] != "Succeeded"))
                      or (c["restarts"] >= 3 and c["last"] and (c["last"].get("ageS") or 1e9) < 3600
                          and (c["last"].get("exitCode") not in (0, None) or c["last"].get("reason") == "OOMKilled"))), None)
        if crash:
            last = crash["last"] or {}
            live = self._ev("Pod", ns, pod, "Unhealthy", contains="Liveness probe failed")
            if live and last.get("reason") != "OOMKilled":
                listening = sorted(self.listen.get(f"{ns}/{pod}", ()))
                cause, fix = A.probe_advice("liveness", live["message"], listening, self._probe_port(n, "livenessProbe"))
                out.append(("liveness", crash["name"], {
                    "category": "probes", "what": "restarted by a failing liveness probe",
                    "klass": "restarted by a failing liveness probe",
                    "cause": "The kubelet kills the container because its liveness probe fails. " + cause, "fix": fix,
                    "evidence": [live["message"]], "since": last.get("ageS"),
                    "detail": f"Container {crash['name']} · {crash['restarts']} restarts.",
                    "covers": ("pod:CrashLoopBackOff", "restarts", "replicas"),
                    "commands": [describe, logs(crash["name"], True)]}))
            else:
                what, cause, fix = A.exit_advice(last.get("exitCode"), last.get("reason", ""))
                if last.get("reason") == "OOMKilled" and p.get("memLimit"):
                    cause += f" Its limit is {p['memLimit'] / 1024 ** 2:.0f}Mi."
                init = "init container " if crash["init"] else ""
                out.append(("crash", f"{crash['name']}:{what}", {
                    "category": "crash", "what": f"crash-looping: {init}{crash['name']} {what}",
                    "klass": f"crash-looping: {what}",
                    "cause": cause + (" Because it is an init container, the main containers never start." if crash["init"] else ""),
                    "fix": fix, "evidence": [last.get("message") or crash["message"]], "since": last.get("ageS"),
                    "detail": f"{crash['restarts']} restarts" + (f", last {dur(last.get('ageS'))} ago" if last.get("ageS") is not None else "") + ".",
                    "covers": ("pod:CrashLoopBackOff", "pod:OOMKilled", "pod:RunContainerError", "restarts", "replicas"),
                    "sizing": ("oom", {"container": crash["name"]}) if last.get("reason") == "OOMKilled" and not crash["init"] else None,
                    "commands": [logs(crash["name"], True), describe]}))
            return out

        # Has a node but never starts its containers.
        age = p.get("ageSeconds") or 0
        if p["phase"] == "Pending" and p["node"] and age > 120:
            running_init = next((c for c in cs if c["init"] and c["state"] == "running"), None)
            if running_init:
                if age > 300:
                    out.append(("init-slow", running_init["name"], {
                        "category": "availability", "severity": "warning",
                        "what": f"waiting on init container {running_init['name']} for {dur(age)}",
                        "cause": "An init container is still running, so the main containers haven't started. Init "
                                 "containers that wait for a dependency (a database, another service) block here.",
                        "fix": "Read the init container's log to see what it waits for.", "since": age,
                        "covers": ("replicas",), "commands": [logs(running_init["name"], False), describe]}))
            else:
                ev = self._ev("Pod", ns, pod, "FailedMount", "FailedAttachVolume", "FailedCreatePodSandBox",
                              "NetworkNotReady", "FailedSync")
                category, what, cause, fix = A.mount_advice((ev or {}).get("reason", ""), (ev or {}).get("message", ""))
                out.append(("creating", what, {
                    "category": category, "what": f"stuck in ContainerCreating for {dur(age)}: {what}", "cause": cause,
                    "klass": f"stuck in ContainerCreating: {what}",
                    "fix": fix, "evidence": [(ev or {}).get("message", "")], "since": age, "covers": ("replicas",),
                    "commands": [describe]}))
            return out

        # Running but not ready.
        if p["phase"] == "Running" and not p["ready"] and (p.get("notReadyS") or 0) > 60 and not p.get("deleting"):
            ev = self._ev("Pod", ns, pod, "Unhealthy", contains="Readiness probe failed") \
                or self._ev("Pod", ns, pod, "Unhealthy", contains="Startup probe failed")
            listening = sorted(self.listen.get(f"{ns}/{pod}", ()))
            if ev:
                kind = "startup" if "Startup" in ev["message"] else "readiness"
                cause, fix = A.probe_advice(kind, ev["message"], listening, self._probe_port(n, f"{kind}Probe"))
                sig = re.sub(r"\d+\.\d+\.\d+\.\d+(:\d+)?", "", ev["message"])[:60]
                out.append(("notready", sig, {
                    "category": "probes", "severity": "critical" if n["replicas"]["ready"] == 0 else "warning",
                    "what": f"running but failing the {kind} probe for {dur(p['notReadyS'])}",
                    "klass": f"running but failing the {kind} probe",
                    "cause": cause + " A pod that isn't ready gets no traffic from its Service.", "fix": fix,
                    "evidence": [ev["message"]], "since": p["notReadyS"], "covers": ("replicas",),
                    "commands": [describe, logs((cs[0]["name"] if cs else pod), False)]}))
            else:
                waiting = [f"{c['name']} ({c['reason'] or c['state']})" for c in cs if not c["ready"] and not c["init"]]
                out.append(("notready", "noprobe", {
                    "category": "availability", "severity": "critical" if n["replicas"]["ready"] == 0 else "warning",
                    "what": f"running but not ready for {dur(p['notReadyS'])}", "klass": "running but not ready",
                    "cause": "Containers not ready: " + (", ".join(waiting) or "unknown") + ".",
                    "fix": "Describe the pod for its conditions and events.", "since": p["notReadyS"],
                    "covers": ("replicas",), "commands": [describe]}))

        # Stuck terminating.
        if (p.get("deletingS") or 0) > 300:
            out.append(("terminating", "x", {
                "category": "availability", "severity": "warning", "what": f"stuck terminating for {dur(p['deletingS'])}",
                "klass": "stuck terminating",
                "cause": "The pod was deleted but can't finish: a finalizer is pending, a container ignores SIGTERM, "
                         "or its node is unreachable so the kubelet never confirms.",
                "fix": "Check the pod's finalizers and its node. Force-delete only if the node is really gone.",
                "since": p["deletingS"],
                "commands": [describe, self.cmd("{cli} -n {ns} get pod {{pod}} -o jsonpath='{{.metadata.finalizers}}'", ns=ns)]}))
        return out

    def _probe_port(self, n: dict, probe: str):
        for c in (n.get("template") or {}).get("containers") or []:
            pr = c.get(probe) or {}
            port = (pr.get("httpGet") or pr.get("tcpSocket") or pr.get("grpc") or {}).get("port")
            if port is not None:
                if isinstance(port, str) and not port.isdigit():
                    port = next((x.get("containerPort") for x in c.get("ports") or [] if x.get("name") == port), port)
                return port
        return None

    def _near_config(self, ns: str, message: str) -> str:
        hit = re.search(r'configmap "([^"]+)" not found', message or "", re.I)
        if not hit:
            return ""
        names = [name_of(c) for c in self.snap.get("configmaps") or [] if ns_of(c) == ns]
        close = difflib.get_close_matches(hit.group(1), names, n=1, cutoff=0.6)
        return f" This namespace has a ConfigMap named \"{close[0]}\" — possibly a typo." if close else ""

    # ---- storage
    def _storage(self) -> None:
        pvcs = {(ns_of(o), name_of(o)): o for o in self.snap.get("persistentvolumeclaims") or []}
        for wid, n in self.workloads():
            claims = sorted({c for p in n["pods"] for c in p.get("claims") or []})
            out = []
            for claim in claims:
                o = pvcs.get((n["namespace"], claim))
                spec, st = (o or {}).get("spec") or {}, (o or {}).get("status") or {}
                out.append({"claim": claim, "phase": st.get("phase", "Missing" if pvcs else "Unknown"),
                            "size": (st.get("capacity") or {}).get("storage") or
                                    ((spec.get("resources") or {}).get("requests") or {}).get("storage", ""),
                            "storageClass": spec.get("storageClassName", ""), "accessModes": spec.get("accessModes") or [],
                            "volume": spec.get("volumeName", "")})
                if o is None and pvcs and any(p["phase"] == "Pending" and claim in (p.get("claims") or []) for p in n["pods"]):
                    self.add("storage", f"pvc-missing:{wid}:{claim}", f"Volume claim {claim} does not exist", subject=wid,
                             weight=0.85, cause="The pod mounts a PersistentVolumeClaim that isn't in this namespace, so it "
                                                "can never be scheduled.",
                             fix="Create the claim or fix claimName in the pod template.",
                             commands=[self.cmd("{cli} -n {ns} get pvc", ns=n["namespace"])], covers=("replicas", "unschedulable"))
            if out:
                n["storage"] = out
        for (ns, name), o in pvcs.items():
            phase = (o.get("status") or {}).get("phase")
            users = sorted(self.claim_users.get((ns, name), ()))
            age = self._age(meta(o).get("creationTimestamp"))
            if phase == "Lost":
                self.add("storage", f"pvc-lost:{ns}/{name}", f"Volume claim {name} lost its volume",
                         subject=users[0] if users else "", namespace=ns, weight=0.85 if users else None,
                         severity="critical", cause="The PersistentVolume this claim was bound to no longer exists.",
                         fix="Restore the volume from backup or recreate the claim (data on the old volume is gone "
                             "unless the PV was retained).",
                         commands=[self.cmd("{cli} -n {ns} describe pvc {name}", ns=ns, name=name)])
            if phase != "Pending":
                continue
            evs = self.obj_events.get(("PersistentVolumeClaim", ns, name), [])
            fail = next((e for e in evs if e["reason"] in ("ProvisioningFailed", "FailedBinding", "VolumeMismatch")), None)
            waiting = any("WaitForFirstConsumer" in e["reason"] or "first consumer" in e["message"] for e in evs) or \
                (not fail and not users)
            if (waiting and not users) or (not fail and (age or 0) < 120):
                continue                                   # normal: binds when a pod first uses it
            msg = (fail or {}).get("message", "")
            sc = (o.get("spec") or {}).get("storageClassName")
            if re.search(r"storageclass.*not found", msg, re.I):
                cause, fix = f"StorageClass \"{sc}\" does not exist.", "Use an existing StorageClass (see the list) or create it."
            elif re.search(r"no persistent volumes available|no storage class is set", msg, re.I):
                cause, fix = ("No StorageClass is set and the cluster has no default one, and no pre-created volume matches.",
                              "Set storageClassName on the claim or mark a StorageClass as default.")
            elif re.search(r"exceeded quota", msg, re.I):
                cause, fix = "The project's storage quota is exhausted.", "Raise the quota or delete unused claims."
            elif msg:
                cause, fix = "The provisioner could not create the volume.", "Read the provisioner's message and check the CSI driver pods."
            else:
                cause, fix = ("No volume has been provisioned yet: the provisioner is slow or not running.",
                              "Check the storage provisioner / CSI driver pods.")
            self.add("storage", f"pvc-pending:{ns}/{name}", f"Volume claim {name} is not bound ({dur(age)})",
                     subject=users[0] if users else "", namespace=ns, weight=0.8 if users else None, severity="critical" if users else "warning",
                     cause=cause, fix=fix, evidence=[msg], since_s=age,
                     detail=f"StorageClass: {sc or '(none)'}" + (f" · used by {len(users)} workload(s)" if users else ""),
                     commands=[self.cmd("{cli} -n {ns} describe pvc {name}", ns=ns, name=name),
                               self.cmd("{cli} get storageclass")], covers=("replicas", "unschedulable"))

    # ---- quota
    def _quotas(self) -> None:
        for q in self.snap.get("resourcequotas") or []:
            ns, name = ns_of(q), name_of(q)
            st = q.get("status") or {}
            hard, used = st.get("hard") or {}, st.get("used") or {}
            rows = []
            for res, h in hard.items():
                hv, uv = _qty(res, h), _qty(res, used.get(res))
                if hv and uv is not None:
                    rows.append((uv / hv, res, str(used.get(res)), str(h)))
            if not rows:
                continue
            rows.sort(reverse=True)
            ratio = rows[0][0]
            self.quota_ratio[ns] = max(self.quota_ratio.get(ns, 0), ratio)
            if ratio < 0.9:
                continue
            tight = [f"{res} {u} of {h} ({round(r * 100)}%)" for r, res, u, h in rows if r >= 0.9][:4]
            f = self.add("quota", f"quota:{ns}/{name}", f"Project quota {round(ratio * 100)}% used: {rows[0][1]}",
                     namespace=ns, severity="critical" if ratio >= 1 else "warning",
                     detail="; ".join(tight),
                     cause="The project's ResourceQuota is " + ("exhausted: new pods (including rollouts and scale-ups) are "
                                                                "refused." if ratio >= 1 else "nearly exhausted: the next "
                                                                "scale-up or rollout surge may be refused."),
                     fix="Raise the quota, or lower requests/limits or replica counts in this project. Rolling updates "
                         "need headroom for the surge pod.",
                     commands=[self.cmd("{cli} -n {ns} describe quota {name}", ns=ns, name=name)])
            if f:
                self.sizing_hints.append((f["id"], "quota", {"ns": ns, "name": name}))

    # ---- services and their wiring
    def _services(self) -> None:
        referenced: dict[str, list[str]] = defaultdict(list)
        for eid, targets in self.b.entry_targets.items():
            for sid, _ in targets:
                referenced[sid].append(eid)
        for sid, svc in self.b.svc.items():
            ns, name = svc["namespace"], svc["name"]
            if ns in self.b.hidden or svc["type"] == "ExternalName":
                continue
            backends = self.b.svc_backends.get(sid, {})
            if svc["selector"] and not backends and not self.b.svc_other_addrs.get(sid):
                used = sid in self.b.nodes or sid in referenced
                want = ", ".join(f"{k}={v}" for k, v in svc["selector"].items())
                near = ""
                best = max(((sum(1 for k, v in svc["selector"].items() if (n.get("labels") or {}).get(k) == v), n)
                            for _w, n in self.workloads() if n["namespace"] == ns), key=lambda x: x[0], default=(0, None))
                if best[1] is not None and 0 < best[0] < len(svc["selector"]):
                    near = f" Closest match: {best[1]['name']} (matches {best[0]} of {len(svc['selector'])} labels)."
                else:                      # nothing matches by label: is there a workload with a similar name?
                    names = {n["name"]: n for _w, n in self.workloads() if n["namespace"] == ns}
                    close = difflib.get_close_matches(name, list(names), n=1, cutoff=0.6)
                    if close:
                        lab = ", ".join(f"{k}={v}" for k, v in list((names[close[0]].get("labels") or {}).items())[:2])
                        near = f" Workload {close[0]} looks related; its pods are labelled {lab}."
                self.add("network", f"svc-empty:{sid}", f"Service {name} selects no pods",
                         subject=sid if sid in self.b.nodes else "", namespace=ns,
                         severity="critical" if used else "info",
                         cause=f"No pod in {ns} has the labels the Service selects ({want}).{near}"
                               + (" Calls and routes to it fail." if used else " Nothing references it right now."),
                         fix="Fix the Service's selector to match the pods' labels, or deploy the workload it expects.",
                         commands=[self.cmd("{cli} -n {ns} get endpoints {name}", ns=ns, name=name),
                                   self.cmd("{cli} -n {ns} get pods --show-labels", ns=ns)], covers=("svc-no-pods",))
                continue
            for wid in backends:
                n = self.b.nodes.get(wid)
                if not n:
                    continue
                containers = (n.get("template") or {}).get("containers") or []
                named = {p.get("name") for c in containers for p in c.get("ports") or [] if p.get("name")}
                for port in svc["ports"]:
                    tp = port.get("targetPort") if port.get("targetPort") is not None else port.get("port")
                    if isinstance(tp, str) and not tp.isdigit():
                        if containers and tp not in named:
                            self.add("network", f"svc-port:{sid}:{tp}",
                                     f"Service {name} targets port name \"{tp}\", which no container defines",
                                     subject=wid, weight=0.7,
                                     cause=f"The Service forwards {port['port']}/{port['protocol']} to a container port "
                                           f"named \"{tp}\", but the pods declare "
                                           + (", ".join(sorted(named)) if named else "no named ports") + ". The Service "
                                           "has no usable endpoints for this port.",
                                     fix="Name the container port to match, or set targetPort to the port number.",
                                     commands=[self.cmd("{cli} -n {ns} describe svc {name}", ns=ns, name=name)])
                        continue
                    if port["protocol"] != "TCP" or tp is None:
                        continue
                    pods = [p for p in n["pods"] if p["ready"] and not p["hostNetwork"] and not p["terminal"]]
                    seen = [self.listen.get(f"{ns}/{p['name']}") for p in pods]
                    seen = [s for s in seen if s]
                    if pods and len(seen) == len(pods) and all(int(tp) not in s for s in seen):
                        listening = sorted(set().union(*seen))[:6]
                        self.add("network", f"svc-port:{sid}:{tp}",
                                 f"Service {name} sends traffic to port {tp}, but the pods don't listen there",
                                 subject=wid, weight=0.7,
                                 cause=f"The pods behind {name} listen on {', '.join(map(str, listening))} (seen on the "
                                       f"node), not on the Service's targetPort {tp}. Every connection through the "
                                       "Service is refused.",
                                 fix=f"Set the Service's targetPort to the port the app listens on, or make the app "
                                     f"listen on {tp}.",
                                 commands=[self.cmd("{cli} -n {ns} describe svc {name}", ns=ns, name=name)])

    # ---- ingress / route wiring
    def _entries(self) -> None:
        def svc_port_ok(svc: dict, port) -> bool:
            if port in (None, ""):
                return True
            return any(str(port) in (str(p.get("port")), str(p.get("name")), str(p.get("targetPort"))) for p in svc["ports"])

        for ing in self.snap.get("ingresses") or []:
            ns, name = ns_of(ing), name_of(ing)
            eid = f"entry:{ns}/Ingress/{name}"
            if eid not in self.b.nodes:
                continue
            spec = ing.get("spec") or {}
            backends = [((spec.get("defaultBackend") or {}).get("service") or {})]
            for rule in spec.get("rules") or []:
                backends += [((p.get("backend") or {}).get("service") or {}) for p in (rule.get("http") or {}).get("paths") or []]
            for b in backends:
                if not b.get("name"):
                    continue
                port = (b.get("port") or {}).get("number") or (b.get("port") or {}).get("name")
                self._entry_backend(eid, "Ingress", ns, name, b["name"], port, svc_port_ok)
            lb = ((ing.get("status") or {}).get("loadBalancer") or {}).get("ingress")
            age = self._age(meta(ing).get("creationTimestamp"))
            if "status" in ing and not lb and (age or 0) > 300:
                cls = spec.get("ingressClassName") or (meta(ing).get("annotations") or {}).get("kubernetes.io/ingress.class")
                self.add("network", f"ingress-unclaimed:{eid}", f"Ingress {name} has no address", subject=eid, weight=0.5,
                         cause="No ingress controller has picked this Ingress up: "
                               + (f"no controller serves ingress class \"{cls}\"." if cls else
                                  "it names no ingressClassName and the cluster has no default class."),
                         fix="Set ingressClassName to a class a running controller serves.",
                         commands=[self.cmd("{cli} get ingressclass"), self.cmd("{cli} -n {ns} describe ingress {name}", ns=ns, name=name)])
        for route in self.snap.get("routes") or []:
            ns, name = ns_of(route), name_of(route)
            eid = f"entry:{ns}/Route/{name}"
            if eid not in self.b.nodes:
                continue
            spec = route.get("spec") or {}
            port = (spec.get("port") or {}).get("targetPort")
            for b in [spec.get("to") or {}] + list(spec.get("alternateBackends") or []):
                if b.get("kind", "Service") == "Service" and b.get("name"):
                    self._entry_backend(eid, "Route", ns, name, b["name"], port, svc_port_ok)
            ingress = (route.get("status") or {}).get("ingress") or []
            for ri in ingress:
                for c in ri.get("conditions") or []:
                    if c.get("type") == "Admitted" and c.get("status") == "False":
                        claimed = c.get("reason") == "HostAlreadyClaimed"
                        self.add("network", f"route-rejected:{eid}", f"Route {name} is not admitted: {c.get('reason', '')}",
                                 subject=eid, weight=0.85, evidence=[c.get("message", "")],
                                 cause=("Another route in a different project already owns this hostname, and the router "
                                        "serves the older one." if claimed else "The router refused this route."),
                                 fix=("Use a different hostname, or delete the older route that claims it."
                                      if claimed else "Read the router's message and fix the route's host/TLS settings."),
                                 commands=[f"oc get routes -A | grep {spec.get('host', name)}",
                                           f"oc -n {ns} describe route {name}"])
            age = self._age(meta(route).get("creationTimestamp"))
            if "status" in route and not ingress and (age or 0) > 120:
                self.add("network", f"route-unclaimed:{eid}", f"Route {name} is not served by any router", subject=eid,
                         weight=0.7, cause="No router has admitted the route: the routers are down, or none selects this "
                                           "route (router sharding by label / namespace).",
                         fix="Check the router pods in openshift-ingress and the IngressController's selectors.",
                         commands=["oc -n openshift-ingress get pods", f"oc -n {ns} describe route {name}"])

    def _entry_backend(self, eid, kind, ns, name, svc_name, port, port_ok) -> None:
        svc = self.b.svc.get(f"svc:{ns}/{svc_name}")
        if svc is None:
            names = [s["name"] for s in self.b.svc.values() if s["namespace"] == ns]
            close = difflib.get_close_matches(svc_name, names, n=1, cutoff=0.6)
            self.add("network", f"entry-nosvc:{eid}:{svc_name}", f"{kind} {name} points at Service {svc_name}, which doesn't exist",
                     subject=eid, weight=0.85,
                     cause=f"No Service named \"{svc_name}\" exists in {ns}, so requests to this {kind.lower()} get a 503."
                           + (f" There is a Service named \"{close[0]}\" — possibly a typo." if close else ""),
                     fix=f"Create the Service or fix the backend name in the {kind}.",
                     commands=[self.cmd("{cli} -n {ns} get svc", ns=ns)])
        elif not port_ok(svc, port):
            have = ", ".join(f"{p.get('name') or ''}{'=' if p.get('name') else ''}{p['port']}" for p in svc["ports"])
            self.add("network", f"entry-port:{eid}:{svc_name}:{port}", f"{kind} {name} uses port {port}, which Service {svc_name} doesn't expose",
                     subject=eid, weight=0.8,
                     cause=f"Service {svc_name} exposes {have}; the {kind.lower()} asks for {port}. Requests get a 503.",
                     fix=f"Point the {kind.lower()} at one of the Service's ports.",
                     commands=[self.cmd("{cli} -n {ns} describe svc {name}", ns=ns, name=svc_name)])

    # ---- NetworkPolicy
    def _network_policies(self) -> None:
        pols: dict[str, list[dict]] = defaultdict(list)
        for p in self.snap.get("networkpolicies") or []:
            pols[ns_of(p)].append(p)
        if not pols:
            return
        ns_labels = {name_of(o): {**labels_of(o), "kubernetes.io/metadata.name": name_of(o)}
                     for o in self.snap.get("namespaces") or []}

        def types(pol: dict) -> set[str]:
            spec = pol.get("spec") or {}
            return set(spec.get("policyTypes") or (["Ingress"] + (["Egress"] if "egress" in spec else [])))

        def peer_ok(peer: dict, pol_ns: str, other: dict) -> bool:
            if "ipBlock" in peer:
                return False
            nsel, psel = peer.get("namespaceSelector"), peer.get("podSelector")
            if nsel is not None:
                labels = ns_labels.get(other["namespace"])
                if labels is None:
                    return True                       # namespace labels unreadable: don't accuse
                if not sel_match(nsel, labels):
                    return False
            elif other["namespace"] != pol_ns:
                return False
            return psel is None or sel_match(psel, other.get("labels") or {})

        def port_ok(rule: dict, ports: list[str]) -> bool:
            rp = rule.get("ports")
            if not rp or not ports:
                return True
            for r in rp:
                if isinstance(r.get("port"), str) and not str(r["port"]).isdigit():
                    return True
                for ep in ports:
                    num, _, proto = ep.partition("/")
                    if not num.isdigit():
                        return True
                    if (r.get("protocol") or "TCP") == (proto or "TCP") and (
                            r.get("port") is None or int(r["port"]) <= int(num) <= int(r.get("endPort") or r["port"])):
                        return True
            return False

        def allowed(selecting: list[dict], rules_key: str, peers_key: str, other: dict, ports: list[str]) -> bool:
            for pol in selecting:
                for rule in (pol.get("spec") or {}).get(rules_key) or []:
                    peers = rule.get(peers_key)
                    if (not peers or any(peer_ok(pe, ns_of(pol), other) for pe in peers)) and port_ok(rule, ports):
                        return True
            return False

        for e in self.edges:
            s, t = self.b.nodes.get(e["source"]), self.b.nodes.get(e["target"])
            if e["type"] != "calls" or not s or not t or s["kind"] != "workload" or t["kind"] != "workload":
                continue
            if not s.get("labels") or not t.get("labels"):
                continue
            pending = e["health"].get("pending")
            if (e["traffic"].get("connections") or e["traffic"].get("rps")) and not pending:
                continue                               # traffic flows: whatever the policies say, it is allowed
            if any(p.get("hostNetwork") for p in s["pods"]):
                continue
            blockers = []
            ing = [p for p in pols.get(t["namespace"], []) if "Ingress" in types(p) and sel_match((p.get("spec") or {}).get("podSelector"), t["labels"])]
            if ing and not allowed(ing, "ingress", "from", s, e["ports"]):
                blockers.append(("ingress to " + t["name"], t["namespace"], [name_of(p) for p in ing]))
            egr = [p for p in pols.get(s["namespace"], []) if "Egress" in types(p) and sel_match((p.get("spec") or {}).get("podSelector"), s["labels"])]
            if egr and not allowed(egr, "egress", "to", t, e["ports"]):
                blockers.append(("egress from " + s["name"], s["namespace"], [name_of(p) for p in egr]))
            if not blockers:
                continue
            what = "; ".join(f"{d} is restricted by {', '.join(names[:3])} ({ns})" for d, ns, names in blockers)
            self.add("network", f"netpol:{e['id']}", f"NetworkPolicy blocks {s['name']} → {t['name']}", subject=e["id"],
                     weight=0.7 if pending else 0.45,
                     cause=f"No rule allows this call: {what}." + (
                         " Connection attempts are going unanswered right now." if pending else
                         " No traffic has been seen on this link."),
                     fix="Add an ingress rule on the callee's policy that admits the caller's pods (podSelector, plus "
                         "namespaceSelector if it is another namespace), and an egress rule on the caller's side if it "
                         "has an egress policy.",
                     commands=[self.cmd("{cli} -n {ns} get networkpolicy", ns=blockers[0][1]),
                               self.cmd("{cli} -n {ns} describe networkpolicy {name}", ns=blockers[0][1], name=blockers[0][2][0])],
                     covers=("pending",))

    # ---- jobs
    def _batch(self) -> None:
        newest: dict[tuple[str, str], str] = {}
        for (ns, name), job in self.b.jobs.items():
            o = controller_ref(job)
            if o and o.get("kind") == "CronJob":
                ts = meta(job).get("creationTimestamp") or ""
                if ts >= newest.get((ns, o["name"]), ""):
                    newest[(ns, o["name"])] = ts
        for (ns, name), job in self.b.jobs.items():
            cond = next((c for c in (job.get("status") or {}).get("conditions") or []
                         if c.get("type") == "Failed" and c.get("status") == "True"), None)
            if not cond:
                continue
            age = self._age(cond.get("lastTransitionTime"))
            if age is not None and age > 86400:
                continue
            o = controller_ref(job)
            cron = o["name"] if o and o.get("kind") == "CronJob" else None
            if cron and (meta(job).get("creationTimestamp") or "") < newest.get((ns, cron), ""):
                continue                                   # a later run exists; only the latest matters
            wid = f"wl:{ns}/CronJob/{cron}" if cron else f"wl:{ns}/Job/{name}"
            reason = cond.get("reason", "")
            cause = {"BackoffLimitExceeded": "The job's pod failed more times than backoffLimit allows.",
                     "DeadlineExceeded": "The job ran longer than its activeDeadlineSeconds and was stopped."}.get(
                reason, cond.get("message", "The job failed."))
            self.add("batch", f"job:{ns}/{name}", f"{'CronJob ' + cron + ': last run' if cron else 'Job ' + name} failed ({reason or 'Failed'})",
                     subject=wid if wid in self.b.nodes else "", namespace=ns, severity="warning", since_s=age,
                     cause=cause, fix="Read the failed pod's log. Failed pods are kept until the job is deleted.",
                     evidence=[cond.get("message", "")],
                     commands=[self.cmd("{cli} -n {ns} logs job/{name} --tail=50", ns=ns, name=name),
                               self.cmd("{cli} -n {ns} describe job {name}", ns=ns, name=name)])
            self.changes.append({"ageS": age, "kind": "job", "subject": wid if wid in self.b.nodes else "", "namespace": ns,
                                 "level": "warning", "title": f"Job {name} failed", "detail": reason})

    # ---- autoscalers
    def _hpa(self) -> None:
        for h in self.snap.get("hpas") or []:
            ns, name = ns_of(h), name_of(h)
            t = (h.get("spec") or {}).get("scaleTargetRef") or {}
            wid = f"wl:{ns}/{t.get('kind')}/{t.get('name')}"
            st = h.get("status") or {}
            for c in st.get("conditions") or []:
                if c.get("type") in ("ScalingActive", "AbleToScale") and c.get("status") == "False" \
                        and c.get("reason") != "ScalingDisabled":
                    f = self.add("saturation", f"hpa:{ns}/{name}", f"Autoscaler {name} can't scale: {c.get('reason', '')}",
                             subject=wid if wid in self.b.nodes else "", namespace=ns, weight=0.3,
                             cause="The HorizontalPodAutoscaler can't compute a replica count, so the workload stays at "
                                   "its current size whatever the load. Usually metrics are missing (no metrics-server / "
                                   "custom metrics adapter) or the pods have no resource requests to measure against.",
                             fix="Check that `top pods` works and that every container sets a request for the metric.",
                             evidence=[c.get("message", "")],
                             commands=[self.cmd("{cli} -n {ns} describe hpa {name}", ns=ns, name=name)])
                    if f and wid in self.b.nodes:
                        self.sizing_hints.append((f["id"], "hpa_stuck", {"wid": wid}))
            age = self._age(st.get("lastScaleTime"))
            if age is not None and age < 7200 and wid in self.b.nodes:
                self.changes.append({"ageS": age, "kind": "scale", "subject": wid, "namespace": ns, "level": "ok",
                                     "title": f"{t.get('name')} autoscaled to {st.get('currentReplicas', '?')} replicas",
                                     "detail": ""})

    # ---- certificates (cert-manager)
    def _certificates(self) -> None:
        for c in self.snap.get("certificates") or []:
            ns, name = ns_of(c), name_of(c)
            st = c.get("status") or {}
            ready = next((x for x in st.get("conditions") or [] if x.get("type") == "Ready"), {})
            left = self._age(st.get("notAfter"))
            left = -left if left is not None else None      # seconds until expiry
            cmd = [self.cmd("{cli} -n {ns} describe certificate {name}", ns=ns, name=name)]
            if left is not None and left < 0:
                self.add("network", f"cert-expired:{ns}/{name}", f"Certificate {name} expired {dur(-left)} ago", namespace=ns,
                         severity="critical", cause="The TLS certificate is past its notAfter date: clients reject the connection.",
                         fix="Find out why cert-manager did not renew it (issuer, DNS/HTTP challenge).", commands=cmd,
                         evidence=[ready.get("message", "")])
            elif ready.get("status") == "False":
                self.add("network", f"cert-notready:{ns}/{name}", f"Certificate {name} is not ready: {ready.get('reason', '')}",
                         namespace=ns, severity="warning", cause="cert-manager has not been able to issue this certificate.",
                         fix="Check the CertificateRequest / Order and the issuer.", commands=cmd, evidence=[ready.get("message", "")])
            elif left is not None and left < 14 * 86400:
                self.add("network", f"cert-expiring:{ns}/{name}", f"Certificate {name} expires in {dur(left)}", namespace=ns,
                         severity="warning", cause="Renewal normally happens well before this. It has not.",
                         fix="Check why cert-manager has not renewed it.", commands=cmd)

    # ---- OpenShift platform
    def _openshift_platform(self) -> None:
        ops = self.snap.get("clusteroperators") or []
        degraded, down, progressing = [], [], []
        for co in ops:
            name = name_of(co)
            conds = {c.get("type"): c for c in (co.get("status") or {}).get("conditions") or []}
            avail, deg, prog = conds.get("Available", {}), conds.get("Degraded", {}), conds.get("Progressing", {})
            if avail.get("status") == "False":
                down.append(name)
                self.add("platform", f"co-down:{name}", f"Cluster operator {name} is unavailable", severity="critical",
                         cause="A core platform component is down. Workloads that depend on it (ingress, DNS, registry, "
                               "storage, authentication ...) fail for reasons that aren't in their own project.",
                         fix="Fix the operator first; application symptoms downstream of it usually clear by themselves.",
                         evidence=[avail.get("message", "")], commands=[f"oc describe clusteroperator {name}", "oc get clusteroperators"])
            elif deg.get("status") == "True":
                degraded.append(name)
                self.add("platform", f"co-degraded:{name}", f"Cluster operator {name} is degraded", severity="warning",
                         cause="A platform component reports a problem but is still serving.",
                         fix="Read the operator's message; check the pods in its namespace.",
                         evidence=[deg.get("message", "")], commands=[f"oc describe clusteroperator {name}"])
            if prog.get("status") == "True":
                progressing.append(name)
        version = None
        for cv in self.snap.get("clusterversions") or []:
            conds = {c.get("type"): c for c in (cv.get("status") or {}).get("conditions") or []}
            desired = ((cv.get("status") or {}).get("desired") or {}).get("version", "")
            version = {"version": desired, "updating": conds.get("Progressing", {}).get("status") == "True",
                       "failing": conds.get("Failing", {}).get("status") == "True"}
            if version["failing"]:
                self.add("platform", "cv-failing", "Cluster version operator reports a failure", severity="critical",
                         cause="The cluster can't reconcile to its target version.", fix="Read the message and the degraded operators.",
                         evidence=[conds["Failing"].get("message", "")], commands=["oc get clusterversion", "oc get clusteroperators"])
            elif version["updating"]:
                self.add("platform", "cv-updating", f"Cluster upgrade in progress (to {desired})", severity="info",
                         cause="During an upgrade, platform pods restart and nodes are drained and rebooted one by one.",
                         fix="Expect pod restarts and brief unavailability of single-replica workloads until it completes.",
                         evidence=[conds["Progressing"].get("message", "")], commands=["oc get clusterversion"])
        pools = []
        for mcp in self.snap.get("machineconfigpools") or []:
            st = mcp.get("status") or {}
            conds = {c.get("type"): c for c in st.get("conditions") or []}
            name = name_of(mcp)
            total, updated = int(st.get("machineCount") or 0), int(st.get("updatedMachineCount") or 0)
            pools.append({"name": name, "machines": total, "updated": updated,
                          "degraded": conds.get("Degraded", {}).get("status") == "True",
                          "updating": conds.get("Updating", {}).get("status") == "True"})
            if pools[-1]["degraded"]:
                self.add("platform", f"mcp-degraded:{name}", f"MachineConfigPool {name} is degraded", severity="critical",
                         cause="A node failed to apply its machine configuration and is stuck (often mid-drain: a pod "
                               "with a PodDisruptionBudget that can't be evicted).",
                         fix="Find the stuck node and what blocks its drain.", evidence=[conds["Degraded"].get("message", "")],
                         commands=[f"oc describe mcp {name}", "oc get nodes"])
            elif pools[-1]["updating"]:
                self.add("platform", f"mcp-updating:{name}", f"Nodes in pool {name} are being updated ({updated}/{total} done)",
                         severity="info", cause="Nodes are cordoned, drained and rebooted one at a time. Pods on them move.",
                         fix="Pod restarts and Pending pods during this window are expected.", commands=[f"oc get mcp {name}", "oc get nodes"])
        if ops or version or pools:
            self.platform["openshift"] = {"operators": len(ops), "degraded": degraded, "unavailable": down,
                                          "progressing": progressing, "version": version, "pools": pools}

    # ---- hardening gaps (information only)
    def _hygiene(self) -> None:
        for wid, n in self.workloads():
            if n.get("platform") or n["subkind"] in ("Job", "CronJob"):
                continue
            containers = (n.get("template") or {}).get("containers") or []
            if not containers:
                continue
            notes = []
            if n["services"] and not any(c.get("readinessProbe") for c in containers):
                notes.append("no readiness probe: the Service sends traffic to pods before they can answer, and keeps "
                             "sending it to a hung pod")
            if any(not ((c.get("resources") or {}).get("limits") or {}).get("memory") for c in containers):
                notes.append("no memory limit: a leak takes memory from every other pod on the node")
            if any(not (c.get("resources") or {}).get("requests") for c in containers):
                notes.append("no resource requests: the scheduler can overpack its node, and these pods are evicted first")
            tags = [c.get("image", "") for c in containers]
            if any("@" not in t and (":" not in t.rsplit("/", 1)[-1] or t.endswith(":latest")) for t in tags):
                notes.append("image tag :latest: rollouts and rollbacks aren't reproducible")
            if notes:
                self.add("hygiene", f"gaps:{wid}", f"{len(notes)} hardening gap{'s' if len(notes) > 1 else ''}",
                         subject=wid, severity="info", detail="; ".join(notes) + ".",
                         cause="Not a failure now. These make the next failure worse or harder to see.",
                         fix="Add the missing probe / requests / limits; pin image tags.")

    # ------------------------------------------------------------------ pass 2 (after scoring)
    def finish(self, graph: dict) -> None:
        nodes = {n["id"]: n for n in graph["nodes"]}
        explained_subjects = {f["subject"] for f in self.findings.values()
                              if f["severity"] != "info" and f["category"] != "saturation"}
        for n in graph["nodes"]:
            risk = n.get("risk") or {}
            if n["kind"] == "host":
                if n.get("pseudo"):
                    continue
                for r in risk.get("reasons", []):
                    tpl = A.HOST.get(r.get("code", ""))
                    if not tpl or r["weight"] < 0.2:
                        continue
                    cause, fix, cmds = tpl
                    evs = [e["message"] for e in n.get("events") or []][:2]
                    sev = "info" if r["code"] in A.HOST_INFO else "critical" if r["weight"] >= 0.6 else "warning"
                    f = self.add("node", f"{r['code']}:{n['id']}", f"Node {n['name']}: {r['text']}", subject=n["id"],
                                 pattern=f"node:{r['code']}",
                                 severity=sev, cause=cause, fix=fix,
                                 commands=[self.cmd(c, name=n["name"]) for c in cmds], evidence=evs)
                    if f:
                        f["weight"] = r["weight"]
                continue
            if n["kind"] != "workload":
                continue
            explained = n["id"] in explained_subjects
            for r in risk.get("reasons", []):
                code = r.get("code", "")
                if code == "diag" or (n["id"], code) in self.covered or r["weight"] < 0.2:
                    continue
                if code == "replicas" and explained:
                    continue
                key = code if code in A.WORKLOAD else "replicas" if code.startswith("pod:") else None
                if not key:
                    continue
                category, cause, fix, cmds, sev = A.WORKLOAD[key]
                f = self.add(category, f"{code}:{n['id']}", r["text"], subject=n["id"], severity=sev, pattern=f"wl:{key}",
                             weight=None, cause=cause, fix=fix,
                             commands=[self.cmd(c, ns=n["namespace"], name=n["name"], ref=self.ref(n)) for c in cmds])
                if f and not sev:
                    f["severity"] = "critical" if r["weight"] >= 0.6 else "warning"
                    f["weight"] = r["weight"]
                    host = (risk.get("impactFrom") or "")
                    if code in ("replicas", "evicted") and host.startswith("host:") and host in nodes and nodes[host]["risk"]["level"] != "ok":
                        f.update(root=False, causedBy=host, causedByName=nodes[host]["name"])
        for e in graph["edges"]:
            s, t = nodes.get(e["source"]), nodes.get(e["target"])
            if not s or not t:
                continue
            for r in e["risk"]["reasons"]:
                code = r.get("code", "")
                if code not in A.EDGE or (e["id"], code) in self.covered or r["weight"] < 0.2:
                    continue
                category, cause, fix = A.EDGE[code]
                info = code in A.EDGE_INFO
                f = self.add(category, f"{code}:{e['id']}", f"{s['name']} → {t['name']}: {r['text']}", subject=e["id"],
                             pattern=f"call:{code}",
                             severity="info" if info else ("critical" if r["weight"] >= 0.6 else "warning"),
                             cause=cause, fix=fix)
                if not f or info:
                    continue
                f["weight"] = r["weight"]
                # A slow or failing call is usually a symptom: point at what is wrong with the callee.
                if code in ("p95", "errors", "sendq", "target-unready", "probe-fail", "connect-slow", "svc-no-pods", "pending"):
                    # Same rule as risk.py's impact attribution: blame the deepest cause that explains a
                    # comparable share - what the callee depends on, before the callee itself.
                    tr = t.get("risk") or {}
                    cands = [((tr.get("impact") or 0) * 0.7, tr.get("impactFrom")), ((tr.get("score") or 0) * 0.7, t["id"])]
                    top = max(v for v, _ in cands)
                    root = next((r for v, r in cands if r and v >= 0.6 * top and v >= 0.17), None)
                    # ...unless the callee itself is what breaks the call: nothing ready, miswired, or saturated.
                    rep = t.get("replicas") or {}
                    breaks = t["kind"] != "workload" or (rep.get("desired", 0) > 0 and rep.get("ready", 0) == 0) \
                        or any(d["category"] == "network" for d in t.get("diag") or []) \
                        or any(x.get("code") in ("cpu", "mem", "accept-queue", "recvq") and x["weight"] >= 0.6
                               for x in tr.get("reasons", []))
                    if breaks and tr.get("level", "ok") != "ok":
                        root = t["id"]
                    if root and root != e["id"]:
                        rn = nodes.get(root)
                        re_ = self.edge_by_id.get(root)
                        name = rn["name"] if rn else (f"{nodes.get(re_['source'], {}).get('name', '?')} → "
                                                      f"{nodes.get(re_['target'], {}).get('name', '?')}" if re_ else root)
                        f.update(root=False, causedBy=root, causedByName=name)

        # Blast radius: how far each root cause reaches.
        by_subject: dict[str, list[dict]] = defaultdict(list)
        for f in self.findings.values():
            f.setdefault("effectOf", None)
            if f["subject"]:
                by_subject[f["subject"]].append(f)
        # On one workload, some findings are the effect of another: a rollout is stuck *because* its new
        # pods can't pull an image; a pod is unschedulable *because* its claim isn't bound.
        causes = ("storage", "admission", "quota", "config", "image", "crash", "probes", "scheduling")
        for fs in by_subject.values():
            why = sorted((f for f in fs if f["category"] in causes and f["severity"] != "info"),
                         key=lambda f: causes.index(f["category"]))
            if not why:
                continue
            for f in fs:
                if f["category"] == "rollout" or (f["category"] == "scheduling" and why[0]["category"] == "storage"
                                                  and "claim" in f["title"]):
                    f["effectOf"] = why[0]["id"]
        for c in graph.get("cascades") or []:
            for f in by_subject.get(c["root"], ()):
                if f["root"] and f["severity"] != "info":
                    f["blast"], f["userFacing"] = c["count"], c["userFacing"]

        self.sizer._guard(self.sizer.after)
        # The whole list stays server-side; each response carries the part its scope needs (see summarize()).
        graph["findings"] = sorted(self.findings.values(), key=lambda f: (
            SEV_RANK[f["severity"]], not f["root"], -int(f["userFacing"]), -f["blast"], -f["weight"], f["title"]))
        graph["changes"] = self._changes(graph)
        graph["platform"] = self._platform_summary(graph)

    def _changes(self, graph: dict) -> list[dict]:
        out = list(self.changes)
        for n in graph["nodes"]:
            if n["kind"] == "workload":
                # Restarts and kills, one line per workload.
                recent = [(c["last"]["ageS"], c, p) for p in n["pods"] for c in p.get("containerInfo") or []
                          if c.get("last") and c["last"].get("ageS") is not None and c["last"]["ageS"] < 7200 and c["restarts"]]
                if recent:
                    age, c, _p = min(recent, key=lambda x: x[0])
                    why = c["last"].get("reason") or "Error"
                    out.append({"ageS": age, "kind": "restart", "subject": n["id"], "namespace": n["namespace"],
                                "level": "warning", "title": f"{n['name']}: container restarted ({why}"
                                + (f", exit {c['last']['exitCode']}" if c["last"].get("exitCode") is not None else "") + ")",
                                "detail": f"{sum(x[1]['restarts'] for x in recent)} restarts across {len({x[2]['name'] for x in recent})} pod(s)"})
                ev = next((e for e in n.get("events") or [] if e["ageS"] is not None), None)
                if ev:
                    out.append({"ageS": ev["ageS"], "kind": "event", "subject": n["id"], "namespace": n["namespace"],
                                "level": "warning", "title": f"{n['name']}: {ev['reason']}" + (f" ×{ev['count']}" if ev["count"] > 1 else ""),
                                "detail": ev["message"][:200]})
        for node in self.snap.get("nodes") or []:
            name = name_of(node)
            for c in (node.get("status") or {}).get("conditions") or []:
                age = self._age(c.get("lastTransitionTime"))
                bad = (c.get("type") == "Ready" and c.get("status") != "True") or \
                    (c.get("type") != "Ready" and c.get("status") == "True")
                if age is not None and age < 86400 and (bad or c.get("type") == "Ready"):
                    state = ("became NotReady" if c.get("status") != "True" else "became Ready") if c.get("type") == "Ready" \
                        else f"reported {c.get('type')}"
                    out.append({"ageS": age, "kind": "node", "subject": f"host:{name}", "namespace": "",
                                "level": "critical" if bad else "ok", "title": f"Node {name} {state}", "detail": c.get("message", "")[:200]})
        out = [c for c in out if c.get("ageS") is not None and c.get("namespace") not in self.b.hidden]
        out.sort(key=lambda c: c["ageS"])
        return out[:MAX_CHANGES]

    def _platform_summary(self, graph: dict) -> dict:
        hosts = [n for n in graph["nodes"] if n["kind"] == "host" and not n.get("pseudo")]
        role = re.compile(r"(?P<dns>coredns|dns-default|kube-dns)|(?P<ingress>router|ingress|traefik|haproxy)|"
                          r"(?P<network>ovn|sdn|calico|cilium|flannel|multus|kube-proxy|canal)|"
                          r"(?P<metrics>metrics-server|prometheus|thanos)|(?P<registry>registry)|"
                          r"(?P<api>apiserver|etcd|controller-manager|scheduler)", re.I)
        components = []
        for n in graph["nodes"]:
            if n["kind"] != "workload" or not n.get("platform"):
                continue
            m = role.search(n["name"])
            if not m or n["risk"]["level"] == "ok":
                continue
            components.append({"id": n["id"], "name": n["name"], "namespace": n["namespace"], "role": m.lastgroup,
                               "level": n["risk"]["level"], "reason": (n["risk"]["reasons"] or [{}])[0].get("text", "")})
        components.sort(key=lambda c: (c["level"] != "critical", c["role"]))
        return {**self.platform, "components": components[:12],
                "nodes": {"total": len(hosts), "ready": sum(1 for h in hosts if h.get("ready")),
                          "pressure": sorted({p for h in hosts for p in h.get("pressure") or []}),
                          "cordoned": sum(1 for h in hosts if h.get("unschedulable"))},
                "cli": self.cli}


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


def _platform_ns(ns: str) -> bool:
    from .topology import PLATFORM_NS
    return bool(PLATFORM_NS.match(ns))


def _template_diff(prev: dict, new: dict) -> list[str]:
    """What changed between two revisions' pod templates. Names only: never values."""
    def spec(rs):
        return (((rs.get("spec") or {}).get("template") or {}).get("spec") or {})

    def ann(rs):
        return ((((rs.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("annotations") or {})

    a = {c.get("name"): c for c in spec(prev).get("containers") or []}
    b = {c.get("name"): c for c in spec(new).get("containers") or []}
    out = []
    for name in b.keys() - a.keys():
        out.append(f"container {name} added")
    for name in a.keys() - b.keys():
        out.append(f"container {name} removed")
    for name in a.keys() & b.keys():
        ca, cb = a[name], b[name]
        if ca.get("image") != cb.get("image"):
            out.append(f"image {name}: {_short_image(ca.get('image', ''))} → {_short_image(cb.get('image', ''))}")
        ea = {e.get("name"): e for e in ca.get("env") or []}
        eb = {e.get("name"): e for e in cb.get("env") or []}
        added, removed = sorted(eb.keys() - ea.keys()), sorted(ea.keys() - eb.keys())
        changed = sorted(k for k in ea.keys() & eb.keys() if ea[k] != eb[k])
        if added:
            out.append(f"env added: {', '.join(added[:4])}")
        if removed:
            out.append(f"env removed: {', '.join(removed[:4])}")
        if changed:
            out.append(f"env changed: {', '.join(changed[:4])}")
        if ca.get("envFrom") != cb.get("envFrom"):
            out.append(f"envFrom changed ({name})")
        ra, rb = ca.get("resources") or {}, cb.get("resources") or {}
        for kind in ("limits", "requests"):
            for res in sorted(set(ra.get(kind) or {}) | set(rb.get(kind) or {})):
                va, vb = (ra.get(kind) or {}).get(res), (rb.get(kind) or {}).get(res)
                if va != vb:
                    out.append(f"{res} {kind[:-1]} {name}: {va or 'none'} → {vb or 'none'}")
        if ca.get("command") != cb.get("command") or ca.get("args") != cb.get("args"):
            out.append(f"command/args changed ({name})")
        for probe in ("readinessProbe", "livenessProbe", "startupProbe"):
            if ca.get(probe) != cb.get(probe):
                out.append(f"{probe[:-5]} probe changed ({name})")
    if spec(prev).get("volumes") != spec(new).get("volumes"):
        out.append("volumes changed")
    if not out:
        if ann(prev).get("kubectl.kubernetes.io/restartedAt") != ann(new).get("kubectl.kubernetes.io/restartedAt"):
            out.append("restarted (rollout restart), no spec change")
        else:
            out.append("pod template metadata changed")
    return out
