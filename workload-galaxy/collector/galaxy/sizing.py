"""Right-sizing advice: concrete ways to fix a workload that is the wrong size.

diagnose.py says *what* is wrong ("OOMKilled", "no node has enough unreserved memory", "the project
quota is exhausted"). This module turns each of those into a short list of options a person can act
on, with numbers taken from the cluster and a command to copy:

    {title, detail, command, admin}        admin: needs someone with cluster-wide rights

Every suggestion is checked against what would stop it working: the project's quota, its
LimitRange, and whether any node still has that much unreserved. Nothing here changes the cluster.

Numbers come from live usage (metrics-server) and the peak this collector has seen since it
started (UsageHistory, kept in memory, up to a day). That is a starting point, not a capacity
plan: each option says what it is based on.
"""
from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict

from . import advice as A
from .util import name_of, ns_of, parse_cpu, parse_mem

log = logging.getLogger("galaxy.sizing")

MI, GI = 1024 ** 2, 1024 ** 3
SETTABLE = {"Deployment", "StatefulSet", "DaemonSet", "DeploymentConfig", "ReplicaSet", "CronJob",
            "ReplicationController"}
SCALABLE = {"Deployment", "StatefulSet", "DeploymentConfig", "ReplicaSet", "ReplicationController"}
MAX_OPTIONS = 5


# ---------------------------------------------------------------------------------------- quantities
def nice_mem(b: float) -> int:
    """Round a byte count up to a value a person would type."""
    for upto, step in ((GI, 64 * MI), (4 * GI, 256 * MI), (16 * GI, 512 * MI), (float("inf"), GI)):
        if b <= upto:
            return int(max(step, math.ceil(b / step - 1e-9) * step))
    return int(b)


def nice_cpu(c: float) -> float:
    step = 0.05 if c <= 1 else 0.25 if c <= 4 else 0.5
    return max(step, round(math.ceil(c / step - 1e-9) * step, 3))


def q_mem(b: float | None, exact: bool = False) -> str:
    """A memory quantity as Kubernetes writes it. exact: for a command, never rounded."""
    if b is None:
        return "?"
    b = int(b)
    if b >= GI and (not exact or (b * 100) % GI == 0):
        return f"{b / GI:.2f}".rstrip("0").rstrip(".") + "Gi"
    return f"{max(1, round(b / MI))}Mi"


def q_cpu(c: float | None, exact: bool = False) -> str:
    if c is None:
        return "?"
    if c >= 1 and (not exact or abs(c * 100 - round(c * 100)) < 1e-6):
        return f"{c:.2f}".rstrip("0").rstrip(".")
    return f"{int(round(c * 1000))}m"


def own(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def span_text(seconds: float) -> str:
    if seconds < 90:
        return "right now"
    m = int(seconds // 60)
    if m < 60:
        return f"over the last {m} min"
    h, m = divmod(m, 60)
    return f"over the last {h}h{f' {m}m' if m and h < 6 else ''}"


def _res_kind(res: str) -> str:
    return "cpu" if "cpu" in res else "memory" if "memory" in res else res


def _q(res: str, v: float | None, exact: bool = False) -> str:
    kind = _res_kind(res)
    return q_cpu(v, exact) if kind == "cpu" else q_mem(v, exact) if kind == "memory" \
        else (f"{int(v)}" if v is not None else "?")


def _say(res: str, v: float | None) -> str:
    kind = _res_kind(res)
    return f"{q_cpu(v)} CPU" if kind == "cpu" else f"{q_mem(v)} of memory" if kind == "memory" else f"{_q(res, v)} {res}"


# ---------------------------------------------------------------------------------------- history
class UsageHistory:
    """Peak CPU and memory per (workload, container), in two-hour buckets for a day.

    Lives in State across refreshes. Small on purpose: a recommendation needs "the most it has
    used lately", not a time series.
    """
    BUCKET, KEEP = 7200, 12

    def __init__(self) -> None:
        self.series: dict[tuple, list[list[float]]] = {}
        self.first: dict[tuple, float] = {}
        self.started = time.time()

    def observe(self, now: float, samples: dict[tuple, tuple[float, float]]) -> None:
        bucket = int(now // self.BUCKET)
        for key, (cpu, mem) in samples.items():
            rows = self.series.get(key)
            if rows is None:
                rows = self.series[key] = []
                self.first[key] = now
            if rows and rows[-1][0] == bucket:
                if cpu > rows[-1][1]:
                    rows[-1][1] = cpu
                if mem > rows[-1][2]:
                    rows[-1][2] = mem
            else:
                rows.append([bucket, cpu, mem])
                if len(rows) > self.KEEP:
                    del rows[0]
        if len(self.series) > len(samples) * 1.5 + 200:        # forget workloads that are gone
            floor = bucket - self.KEEP
            for key in [k for k, rows in self.series.items() if rows[-1][0] < floor]:
                del self.series[key]
                self.first.pop(key, None)

    def peak(self, key: tuple, now: float | None = None) -> tuple[float, float, float] | None:
        rows = self.series.get(key)
        if not rows:
            return None
        now = now or time.time()
        span = min(now - self.first.get(key, now), self.BUCKET * self.KEEP)
        return max(r[1] for r in rows), max(r[2] for r in rows), max(0.0, span)


# ---------------------------------------------------------------------------------------- the advisor
class Sizer:
    def __init__(self, diag, history: UsageHistory | None = None, *, min_history: float = 3600.0):
        self.d = diag
        self.b = diag.b
        self.nodes = diag.b.nodes
        self.snap = diag.snap
        self.cli = diag.cli
        self.history = history if history is not None else UsageHistory()
        self.min_history = min_history
        self.now = time.time()
        self.wl: dict[str, dict] = {}
        self.hosts: list[dict] = []
        self.quotas: dict[str, list[dict]] = defaultdict(list)
        self.ranges: dict[str, list[dict]] = defaultdict(list)
        self.on_host: dict[str, list[tuple[str, dict]]] = defaultdict(list)
        self.oom: set[str] = set()
        self._slack_cache: dict[tuple, list[dict]] = {}
        self._thin = False
        self._host_done: set[tuple[str, str]] = set()

    # ------------------------------------------------------------------ entry points
    def run(self) -> None:
        """Before risk scoring: options for the findings diagnose.py flagged, plus saturation findings."""
        self._index()
        for fid, kind, info in self.d.sizing_hints:
            f = self.d.findings.get(fid)
            if f and not f.get("options"):
                self._guard(getattr(self, f"_h_{kind}"), f, info)
        for step in (self._saturation, self._hpa_max, self._oversized):
            self._guard(step)

    def after(self) -> None:
        """After risk scoring: options for the findings finish() derived from risk signals."""
        for f in list(self.d.findings.values()):
            if f.get("options"):
                continue
            pattern = f.get("pattern", "")
            if f["category"] == "node" and pattern.startswith("node:"):
                self._guard(self._host, f, pattern[5:])
            elif pattern == "wl:evicted":
                self._guard(self._evicted, f)

    def _guard(self, fn, *args) -> None:
        self._thin = False
        try:                                    # advice must never take the board down
            fn(*args)
        except Exception:                       # noqa: BLE001
            log.exception("sizing step %s failed", getattr(fn, "__name__", fn))

    # ------------------------------------------------------------------ index
    def _index(self) -> None:
        mc = getattr(self.b, "metrics_c", {})
        samples: dict[tuple, tuple[float, float]] = {}
        for wid, n in self.d.workloads():
            cs = self._containers(n)
            use: dict[str, tuple[float, float]] = {}
            live = 0
            for p in n["pods"]:
                if p["terminal"]:
                    continue
                if p.get("hostId"):
                    self.on_host[p["hostId"]].append((wid, p))
                if p["phase"] != "Running":
                    continue
                live += 1
                rows = mc.get((n["namespace"], p["name"])) or []
                for i, (cname, cpu, mem) in enumerate(rows):
                    name = cname or (cs[i]["name"] if i < len(cs) else "")
                    if len(rows) == 1 and len(cs) == 1:
                        name = cs[0]["name"]
                    a = use.get(name, (0.0, 0.0))
                    use[name] = (max(a[0], cpu), max(a[1], mem))
            for cname, v in use.items():
                samples[(wid, cname)] = v
            self.wl[wid] = {"containers": cs, "use": use, "live": live}
        self.history.observe(self.now, samples)

        for h in self.nodes.values():
            if h["kind"] != "host" or h.get("pseudo") or not h.get("ready") or h.get("unschedulable"):
                continue
            if any(t.endswith(":NoSchedule") or t.endswith(":NoExecute") for t in h.get("taints") or []):
                continue
            if h.get("cpuAllocatable") is None or h.get("memAllocatable") is None:
                continue
            self.hosts.append({"name": h["name"], "id": h["id"],
                               "cpuFree": max(0.0, h["cpuAllocatable"] - h.get("cpuRequested", 0)),
                               "memFree": max(0.0, h["memAllocatable"] - h.get("memRequested", 0)),
                               "cpu": h["cpuAllocatable"], "mem": h["memAllocatable"]})

        for q in self.snap.get("resourcequotas") or []:
            st = q.get("status") or {}
            hard, used = st.get("hard") or (q.get("spec") or {}).get("hard") or {}, st.get("used") or {}
            row = {"name": name_of(q), "hard": {}, "used": {}}
            for res, h in hard.items():
                key = {"cpu": "requests.cpu", "memory": "requests.memory"}.get(res, res)
                row["hard"][key] = _parse(res, h)
                row["used"][key] = _parse(res, used.get(res)) or 0.0
            self.quotas[ns_of(q)].append(row)
        for lr in self.snap.get("limitranges") or []:
            for item in (lr.get("spec") or {}).get("limits") or []:
                if item.get("type") == "Container":
                    self.ranges[ns_of(lr)].append({
                        "name": name_of(lr),
                        "max": {k: _parse(k, v) for k, v in (item.get("max") or {}).items()},
                        "min": {k: _parse(k, v) for k, v in (item.get("min") or {}).items()}})

    @staticmethod
    def _containers(n: dict) -> list[dict]:
        out = []
        for c in (n.get("template") or {}).get("containers") or []:
            r = c.get("resources") or {}
            req, lim = r.get("requests") or {}, r.get("limits") or {}
            out.append({"name": c.get("name", ""), "image": c.get("image", ""),
                        "cpuReq": parse_cpu(req.get("cpu")), "memReq": parse_mem(req.get("memory")),
                        "cpuLim": parse_cpu(lim.get("cpu")), "memLim": parse_mem(lim.get("memory"))})
        return out

    # ------------------------------------------------------------------ helpers
    def _container(self, wid: str, name: str | None = None, by: str = "mem") -> dict | None:
        w = self.wl.get(wid)
        if not w or not w["containers"]:
            return None
        if name:
            hit = next((c for c in w["containers"] if c["name"] == name), None)
            if hit:
                return hit
        i = 0 if by == "cpu" else 1
        return max(w["containers"], key=lambda c: (w["use"].get(c["name"], (0, 0))[i],
                                                   c.get("cpuReq" if by == "cpu" else "memReq") or 0))

    def _peak(self, wid: str, cname: str) -> tuple[float, float, float] | None:
        return self.history.peak((wid, cname), self.now)

    def _basis(self, span: float) -> str:
        if span < 600:
            self._thin = True               # one reading is not a peak: the finding says so once
            return "right now"
        return f"at its peak {span_text(span)}"

    def _pods(self, n: dict) -> int:
        return max(1, n["replicas"].get("desired") or 0, self.wl.get(n["id"], {}).get("live", 0))

    def _ref(self, n: dict) -> str:
        return self.d.ref(n)

    def _set_cmd(self, n: dict, cname: str, *, req: dict | None = None, lim: dict | None = None) -> str:
        """`set resources` for the controller, or "" where the pod template can't be changed in place."""
        if n.get("subkind") not in SETTABLE:
            return ""
        parts = [f"{self.cli} -n {n['namespace']} set resources {self._ref(n)} -c {cname}"]
        for flag, vals in (("--requests", req), ("--limits", lim)):
            vals = {k: v for k, v in (vals or {}).items() if v is not None}
            if vals:
                parts.append(f"{flag}=" + ",".join(f"{k}={_q(k, v, True)}" for k, v in vals.items()))
        return " ".join(parts) if len(parts) > 1 else ""

    def _fits(self, cpu: float, mem: float) -> int:
        return sum(1 for h in self.hosts if h["cpuFree"] >= cpu - 1e-9 and h["memFree"] >= mem - 1e-9)

    def _pod_request(self, wid: str, override: dict | None = None) -> tuple[float, float]:
        """What one pod of this workload reserves, with one container's values replaced."""
        cpu = mem = 0.0
        for c in self.wl.get(wid, {}).get("containers", []):
            o = override if override and override.get("name") == c["name"] else {}
            cpu += o.get("cpuReq", c["cpuReq"]) or 0
            mem += o.get("memReq", c["memReq"]) or 0
        return cpu, mem

    def _room(self, n: dict, c: dict, *, cpu_req=None, mem_req=None, cpu_lim=None, mem_lim=None,
              extra_pods: int = 0) -> str:
        """What would stop a change from working: quota, LimitRange, node capacity. "" if nothing would."""
        ns, pods = n["namespace"], self._pods(n)
        new = {"requests.cpu": cpu_req, "requests.memory": mem_req, "limits.cpu": cpu_lim, "limits.memory": mem_lim}
        cur = {"requests.cpu": c["cpuReq"], "requests.memory": c["memReq"], "limits.cpu": c["cpuLim"],
               "limits.memory": c["memLim"]}
        notes = []
        for q in self.quotas.get(ns, ()):
            for res, hard in q["hard"].items():
                if hard is None or res not in new:
                    continue
                value = new[res] if new[res] is not None else cur[res]
                delta = ((new[res] - (cur[res] or 0)) * pods if new[res] is not None else 0) + (value or 0) * extra_pods
                left = hard - q["used"].get(res, 0)
                if delta > left + 1e-9:
                    notes.append(f"Quota {q['name']} has only {_q(res, max(0, left))} of {res} left and this needs "
                                 f"{_q(res, delta)} more: the quota has to be raised too.")
                    break
        for lr in self.ranges.get(ns, ()):
            for res, value in (("cpu", cpu_lim), ("memory", mem_lim)):
                cap = lr["max"].get(res)
                if value is not None and cap is not None and value > cap + 1e-9:
                    notes.append(f"LimitRange {lr['name']} caps a container at {_q(res, cap)} {res}: that has to be "
                                 f"raised first, or the new pods are refused.")
        if self.hosts and (cpu_req is not None or mem_req is not None or extra_pods):
            pc, pm = self._pod_request(n["id"], {"name": c["name"], **({"cpuReq": cpu_req} if cpu_req is not None else {}),
                                                 **({"memReq": mem_req} if mem_req is not None else {})})
            biggest_c, biggest_m = max(h["cpu"] for h in self.hosts), max(h["mem"] for h in self.hosts)
            if pc > biggest_c or pm > biggest_m:
                notes.append(f"No node is that big (the largest has {q_cpu(biggest_c)} CPU / {q_mem(biggest_m)} "
                             f"allocatable): the pods would never schedule.")
            elif not self._fits(pc, pm):
                notes.append(f"No node has {q_cpu(pc)} CPU / {q_mem(pm)} unreserved right now: new pods would wait "
                             f"in Pending until capacity is freed or added.")
            elif extra_pods:
                can = self._capacity(pc, pm)
                if can < extra_pods:
                    notes.append(f"The nodes have room for about {can} more pod{'s' if can != 1 else ''} of this size, "
                                 f"not {extra_pods}.")
        return " ".join(notes)

    def _capacity(self, cpu: float, mem: float) -> int:
        """How many more pods of this size the schedulable nodes could take."""
        total = 0
        for h in self.hosts:
            by_cpu = h["cpuFree"] / cpu if cpu > 0 else 1e9
            by_mem = h["memFree"] / mem if mem > 0 else 1e9
            total += int(min(by_cpu, by_mem, 500))
        return total

    def _managed(self, n: dict) -> str:
        # Who owns the spec is written on the controller object (Helm and Argo CD annotate it), and
        # often on the pods too.
        obj = (getattr(self.b, "controllers", {}).get((n["namespace"], n.get("subkind", ""), n["name"])) or {}).get("metadata") or {}
        labels = {**(n.get("labels") or {}), **(obj.get("labels") or {})}
        notes = obj.get("annotations") or {}
        by = labels.get("app.kubernetes.io/managed-by", "")
        if by.lower() == "helm" or labels.get("helm.sh/chart") or notes.get("meta.helm.sh/release-name"):
            release = notes.get("meta.helm.sh/release-name") or labels.get("helm.sh/chart", "")
            return (f"Deployed with Helm{f' ({release})' if release else ''}: put the new values in the chart's "
                    f"values file too, or the next upgrade puts the old ones back.")
        if any(k.startswith("argocd.argoproj.io/") for k in (*labels, *notes)):
            return "Synced by Argo CD: change the values in Git, or the next sync reverts a manual edit."
        if by and by.lower() not in ("", "kubectl", "oc"):
            return f"Managed by {by}: change it there, or a manual edit may be reverted."
        if n.get("subkind") in ("Job", "Pod"):
            return ("A running Job or bare pod can't be resized in place: change the values where it is defined "
                    "and start it again.")
        return ""

    def _runtime_hint(self, c: dict) -> dict | None:
        image = (c.get("image") or "").lower()
        if re.search(r"java|jdk|jre|openjdk|temurin|spring|tomcat|wildfly|jboss|kafka|elasticsearch|cassandra|keycloak|"
                     r"liberty|quarkus", image):
            return _opt("Make the JVM size itself from the limit",
                        "A JVM with a fixed -Xmx bigger than the limit (or an old default) is killed however "
                        "healthy it is. Set -XX:MaxRAMPercentage=75 (or keep -Xmx about 25% under the limit) so "
                        "heap plus metaspace and threads fit.")
        if re.search(r"node|nodejs|next|nest", image):
            return _opt("Tell Node.js how much heap it may use",
                        "Node does not read the container limit for its heap: set --max-old-space-size to about "
                        "75% of the limit in MiB, or it either wastes the limit or blows through it.")
        if re.search(r"postgres|mysql|mariadb|redis|mongo|memcached", image):
            return _opt("Check the datastore's own memory setting",
                        "shared_buffers / innodb_buffer_pool_size / maxmemory / cacheSizeGB is set independently "
                        "of the container limit and must fit under it with room for connections.")
        if re.search(r"golang|/go[-:]|-go[-:]", image):
            return _opt("Give the Go runtime a soft limit",
                        "Set GOMEMLIMIT to about 90% of the container limit so the garbage collector works harder "
                        "before the kernel steps in.")
        return None

    def _attach(self, f: dict, options: list[dict | None], n: dict | None = None) -> None:
        opts = [o for o in options if o][:MAX_OPTIONS]
        if not opts:
            return
        f["options"] = opts
        notes = [self._managed(n) if n else ""]
        if self._thin:
            notes.append("Sized from a single reading: this dashboard has only just started watching these pods. "
                         "After an hour or so the numbers use the peak instead.")
        self._thin = False
        if any(notes):
            f["optionsNote"] = " ".join(x for x in notes if x)

    def _add_capacity(self, what: str) -> dict:
        if self.d.openshift:
            return _opt("Add a worker node", what + " Scale the worker MachineSet (or let the cluster autoscaler do it).",
                        "oc -n openshift-machine-api get machinesets", admin=True)
        return _opt("Add a node", what + " In Rancher: Cluster Management → the cluster → add a machine to the node pool.",
                    f"{self.cli} get nodes -o custom-columns=NAME:.metadata.name,CPU:.status.allocatable.cpu,"
                    f"MEMORY:.status.allocatable.memory", admin=True)

    # ------------------------------------------------------------------ slack: who reserves what they don't use
    def _slack(self, res: str, field: str, *, ns: str | None = None, host: str | None = None,
               exclude: str | None = None) -> list[dict]:
        """Workloads holding the most of `res` they don't use. field: "Req" (reservations) or "Lim" (limits)."""
        key = (res, field, ns, host)
        if key not in self._slack_cache:
            i = 0 if res == "cpu" else 1
            attr = ("cpu" if res == "cpu" else "mem") + field
            rows = []
            for wid, w in self.wl.items():
                n = self.nodes[wid]
                if ns and n["namespace"] != ns:
                    continue
                pods = sum(1 for p in n["pods"] if not p["terminal"] and p["phase"] == "Running"
                           and (not host or p.get("hostId") == host))
                if not pods:
                    continue
                for c in w["containers"]:
                    held, pk = c.get(attr), self._peak(wid, c["name"])
                    if not held or not pk or pk[i] <= 0:
                        continue
                    # A limit needs more air above the peak than a request does.
                    k = (2.0 if res == "cpu" else 1.5) if field == "Lim" else (1.4 if res == "cpu" else 1.3)
                    target = nice_cpu(pk[i] * k) if res == "cpu" else nice_mem(pk[i] * k)
                    if target >= held * 0.75:
                        continue
                    rows.append({"wid": wid, "name": n["name"], "namespace": n["namespace"], "container": c["name"],
                                 "pods": pods, "held": held, "peak": pk[i], "span": pk[2], "target": target,
                                 "saving": (held - target) * pods})
            rows.sort(key=lambda r: -r["saving"])
            self._slack_cache[key] = rows[:12]
        return [r for r in self._slack_cache[key] if r["wid"] != exclude][:3]

    def _reclaim(self, res: str, field: str, *, ns: str | None = None, host: str | None = None,
                 exclude: str | None = None, need: float | None = None) -> dict | None:
        rows = self._slack(res, field, ns=ns, host=host, exclude=exclude)
        if not rows:
            return None
        word = "reserves" if field == "Req" else "is limited to"
        lines = [f"{r['name']}{'' if ns else ' (' + r['namespace'] + ')'}: {r['pods']} pod{'s' if r['pods'] != 1 else ''}, "
                 f"each {word} {_q(res, r['held'])} and peaked at {_q(res, r['peak'])} → {_q(res, r['target'])} would free "
                 f"{_q(res, r['saving'])}" for r in rows]
        total = sum(r["saving"] for r in rows)
        top = rows[0]
        kw = {"req" if field == "Req" else "lim": {res: top["target"]}}
        where = "in this project" if ns else "on this node" if host else "in the cluster"
        enough = "" if need is None else (" That is enough to cover what is missing." if total >= need
                                         else f" That covers {_q(res, total)} of the {_q(res, need)} missing.")
        return _opt(f"Take back {_say(res, total)} that is held but not used {where}",
                    "; ".join(lines) + "." + enough + f" Based on usage {span_text(min(r['span'] for r in rows))}: "
                    "check a busy period before cutting.",
                    self._set_cmd(self.nodes[top["wid"]], top["container"], **kw))

    # ------------------------------------------------------------------ handlers for flagged findings
    def _h_oom(self, f: dict, info: dict) -> None:
        wid = info["wid"]
        n, c = self.nodes[wid], self._container(wid, info.get("container"))
        if not c:
            return
        self.oom.add(wid)
        self.d.covered.add((wid, "mem"))
        pk = self._peak(wid, c["name"])
        seen = pk[1] if pk else 0
        limit = c["memLim"]
        opts: list[dict | None] = []
        if not limit:
            new = nice_mem(max(seen * 1.5, 256 * MI))
            req = nice_mem(max(seen, 128 * MI))
            opts.append(_opt(
                f"Give {c['name']} an explicit memory request and limit ({q_mem(req)} / {q_mem(new)})",
                "The container sets no memory limit of its own, so the kill came from a namespace default or from "
                "the node running out. " + (f"It used {q_mem(seen)} {self._basis(pk[2])}." if seen else
                                            "There is no usage to measure yet, so these are conservative starting values."),
                self._set_cmd(n, c["name"], req={"memory": req}, lim={"memory": new}),
                note=self._room(n, c, mem_req=req, mem_lim=new)))
        else:
            new = nice_mem(max(limit * 1.5, seen * 1.3))
            req = c["memReq"]
            new_req = new if req and abs(req - limit) < MI else (min(new, nice_mem(seen)) if seen > (req or 0) else None)
            used = f" Its pods reached {q_mem(seen)} ({round(100 * seen / limit)}% of the limit) {self._basis(pk[2])}." \
                if seen else ""
            opts.append(_opt(
                f"Raise {own(c['name'])} memory limit from {q_mem(limit)} to {q_mem(new)}",
                f"The kernel kills the container the moment it crosses {q_mem(limit)}.{used} Half as much again gives "
                f"it room to show where it levels off"
                + (f"; the request moves to {q_mem(new_req)} so the scheduler reserves what it really uses." if new_req
                   else "."),
                self._set_cmd(n, c["name"], req={"memory": new_req} if new_req else None, lim={"memory": new}),
                note=self._room(n, c, mem_req=new_req, mem_lim=new)))
        opts.append(self._runtime_hint(c))
        opts.append(_opt("If memory climbs steadily until every kill, it is a leak",
                         "More memory only stretches the time between restarts. Watch the pods after the change: "
                         "a healthy service flattens out, a leak keeps rising.",
                         f"{self.cli} -n {n['namespace']} top pods --containers | grep {n['name']}"))
        self._attach(f, opts, n)

    def _h_sched(self, f: dict, info: dict) -> None:
        wid, p, count = info["wid"], info["pod"], info.get("count", 1)
        n = self.nodes[wid]
        msg = info.get("message", "")
        short = [r for r in ("cpu", "memory") if re.search(rf"Insufficient {r}", msg, re.I)]
        if not short or not self.hosts:
            return
        req_c, req_m = p.get("cpuRequest") or 0.0, p.get("memRequest") or 0.0
        opts: list[dict | None] = []
        facts = []
        for res in short:
            req = req_c if res == "cpu" else req_m
            key = "cpuFree" if res == "cpu" else "memFree"
            best = max(self.hosts, key=lambda h: h[key])
            biggest = max(h["cpu" if res == "cpu" else "mem"] for h in self.hosts)
            if best[key] >= req:
                # By our count it would fit: tainted or cordoned nodes, or a message older than the last change.
                facts.append(f"By this dashboard's count {best['name']} has {_say(res, best[key])} unreserved, which "
                             f"would hold the {_q(res, req)} each pod asks for: the scheduler's message may predate "
                             f"a change. Describe the pod for its current reason.")
                continue
            facts.append(f"Each pod asks for {_say(res, req)}. The most any node has unreserved is "
                         f"{_q(res, best[key])} ({best['name']}); the largest node has {_q(res, biggest)} in all.")
            c = self._container(wid, None, by="cpu" if res == "cpu" else "mem")
            if not c:
                continue
            i = 0 if res == "cpu" else 1
            pk = self._peak(wid, c["name"])
            attr = "cpuReq" if res == "cpu" else "memReq"
            # The pending pod is the truth (a rollout may have changed the template): what its other
            # containers reserve is taken off to get this container's share.
            others = sum(x.get(attr) or 0 for x in self.wl[wid]["containers"] if x["name"] != c["name"])
            cur = max(0.0, req - others) or (c.get(attr) or 0)
            room = best[key] - others
            if pk and pk[i] > 0:
                want = nice_cpu(pk[i] * 1.25) if res == "cpu" else nice_mem(pk[i] * 1.2)
                if want < cur:
                    pc, pm = (others + want, req_m) if res == "cpu" else (req_c, others + want)
                    fit = self._fits(pc, pm)
                    opts.append(_opt(
                        f"Lower {own(c['name'])} {res} request from {_q(res, cur)} to {_q(res, want)}",
                        f"The running pods of this workload used at most {_q(res, pk[i])} {self._basis(pk[2])}. "
                        + (f"A request of {_q(res, want)} fits on {fit} of {len(self.hosts)} nodes today."
                           if fit else f"Even {_q(res, want)} doesn't fit anywhere today, so capacity is needed as well.")
                        + " A request is what the scheduler reserves, not a cap on what the pod may use.",
                        self._set_cmd(n, c["name"], req={res: want})))
                else:
                    facts.append(f"Its running pods really do use about that much ({_q(res, pk[i])} "
                                 f"{self._basis(pk[2])}), so the request is honest: this needs capacity, not a smaller request.")
            elif room > 0 and cur > room:
                fit_now = (math.floor(room / 0.05) * 0.05) if res == "cpu" else (math.floor(room / (64 * MI)) * 64 * MI)
                if fit_now > 0:
                    opts.append(_opt(
                        f"Lower {own(c['name'])} {res} request to {_q(res, fit_now)} or less",
                        f"No pod of this workload is running, so there is no usage to size it from. Anything up to "
                        f"{_q(res, fit_now)} would be placed today. Check what it needs before cutting.",
                        self._set_cmd(n, c["name"], req={res: fit_now})))
            if req > biggest:
                opts.insert(0, _opt(f"No node is big enough for {_say(res, req)}",
                                    f"Freeing reservations won't help: the largest node has {_q(res, biggest)} in total. "
                                    f"Either the request is a mistake, or the cluster needs a larger machine type.",
                                    admin=True))
            opts.append(self._reclaim(res, "Req", exclude=wid, need=max(0.0, req - best[key]) * count))
        if not opts and not any("Each pod asks" in x for x in facts):
            if facts:
                f["detail"] = (f.get("detail", "") + " " + " ".join(facts)).strip()
            return
        need = " and ".join(_say(r, (req_c if r == "cpu" else req_m) * count) for r in short)
        opts.append(self._add_capacity(f"{count} pending pod{'s' if count != 1 else ''} need{'s' if count == 1 else ''} "
                                       f"{need} that no node has free."))
        if facts:
            f["detail"] = " ".join(facts)
        self._attach(f, opts, n)

    def _h_quota_create(self, f: dict, info: dict) -> None:
        wid, msg = info["wid"], info.get("message", "")
        n = self.nodes[wid]
        ns = n["namespace"]
        must = re.search(r"must specify ([\w.,]+)", msg)
        if must:
            c = self._container(wid)
            if not c:
                return
            pk = self._peak(wid, c["name"])
            cpu, mem = (pk[0], pk[1]) if pk else (0.0, 0.0)
            req = {"cpu": c["cpuReq"] or nice_cpu(max(cpu * 1.25, 0.05)), "memory": c["memReq"] or nice_mem(max(mem * 1.2, 128 * MI))}
            lim = {"cpu": c["cpuLim"] or nice_cpu(max(cpu * 2, 0.25)), "memory": c["memLim"] or nice_mem(max(mem * 1.5, 256 * MI))}
            self._attach(f, [
                _opt(f"Set the values the quota asks for on {c['name']}",
                     f"The project's quota counts {must.group(1).replace(',', ', ')}, so every container must declare "
                     f"them. " + (f"Sized from its usage {self._basis(pk[2])}." if pk else
                                  "No usage to measure yet: these are conservative starting values."),
                     self._set_cmd(n, c["name"], req=req, lim=lim), note=self._room(n, c, cpu_req=req["cpu"],
                                                                                   mem_req=req["memory"], cpu_lim=lim["cpu"],
                                                                                   mem_lim=lim["memory"])),
                _opt("Or give the project defaults", "A LimitRange with default requests and limits fills them in for "
                     "every container that doesn't set its own.", f"{self.cli} -n {ns} get limitrange", admin=True)], n)
            return
        m = re.search(r"exceeded quota: ([^,]+), requested: (.+?), used: (.+?), limited: (\S+)", msg)
        if not m:
            return
        qname = m.group(1).strip()
        parts = [dict(kv.split("=", 1) for kv in grp.split(",") if "=" in kv) for grp in m.group(2, 3, 4)]
        missing = max(1, (n["replicas"].get("desired") or 0) - sum(1 for p in n["pods"] if not p["terminal"]))
        opts: list[dict | None] = []
        raise_to, facts = {}, []
        for res, hard_s in parts[2].items():
            hard, used, req = _parse(res, hard_s), _parse(res, parts[1].get(res)), _parse(res, parts[0].get(res))
            if hard is None or used is None or req is None:
                continue
            left = max(0.0, hard - used)
            need = req * missing
            facts.append(f"{res}: {_q(res, left)} left of {_q(res, hard)}; each new pod needs {_q(res, req)}"
                         + (f" and {missing} are missing ({_q(res, need)})." if missing > 1 else "."))
            raise_to[res] = (nice_cpu if _res_kind(res) == "cpu" else nice_mem if _res_kind(res) == "memory"
                             else math.ceil)((used + need) * 1.15)
        if not raise_to:
            return
        hard_json = ",".join(f'"{res}":"{_q(res, v, True)}"' for res, v in raise_to.items())
        opts.append(_opt(
            f"Raise quota {qname} to " + ", ".join(f"{_q(res, v)} {res}" for res, v in raise_to.items()),
            f"Covers what is running plus the {missing} pod{'s' if missing != 1 else ''} that can't be created, with "
            f"15% to spare for the surge pod of the next rollout. Project owners usually can't edit their own quota.",
            f"{self.cli} -n {ns} patch resourcequota {qname} --type merge -p '{{\"spec\":{{\"hard\":{{{hard_json}}}}}}}'",
            admin=True))
        for res in raise_to:
            kind, field = _res_kind(res), "Lim" if res.startswith("limits.") else "Req"
            if kind not in ("cpu", "memory"):
                continue
            used, hard, req = (_parse(res, parts[i].get(res)) for i in (1, 2, 0))
            opts.append(self._reclaim(kind, field, ns=ns, need=max(0.0, req * missing - (hard - used))))
            c = self._container(wid, None, by="cpu" if kind == "cpu" else "mem")
            pk = self._peak(wid, c["name"]) if c else None
            cur = c and c.get(("cpu" if kind == "cpu" else "mem") + field)
            if c and pk and cur:
                i = 0 if kind == "cpu" else 1
                want = (nice_cpu(pk[i] * 1.5) if kind == "cpu" else nice_mem(pk[i] * 1.35)) if field == "Lim" \
                    else (nice_cpu(pk[i] * 1.25) if kind == "cpu" else nice_mem(pk[i] * 1.2))
                pods = self._pods(n)
                if want < cur * 0.8 and want * pods <= hard - used + cur * (pods - missing) + 1e-9:
                    opts.append(_opt(
                        f"Make {own(n['name'])} own pods smaller: {res} {_q(res, cur)} → {_q(res, want)}",
                        f"Its running pods peaked at {_q(res, pk[i])} {span_text(pk[2])}. At {_q(res, want)} each, all "
                        f"{pods} pods fit inside the quota as it is.",
                        self._set_cmd(n, c["name"], **{"lim" if field == "Lim" else "req": {kind: want}})))
        live = sum(1 for p in n["pods"] if not p["terminal"])
        if n.get("subkind") == "Deployment" and live >= (n["replicas"].get("desired") or 0) > 0:
            opts.append(_opt(
                "Roll out without a surge pod",
                "Every pod the Deployment wants is running; what the quota refuses is the extra pod a rolling update "
                "starts before stopping an old one. With maxSurge 0 the rollout replaces pods one at a time and "
                "needs no extra quota (at the cost of one pod less while it runs).",
                f"{self.cli} -n {ns} patch {self._ref(n)} -p "
                "'{\"spec\":{\"strategy\":{\"rollingUpdate\":{\"maxSurge\":0,\"maxUnavailable\":1}}}}'"))
        if facts:
            f["detail"] = (f.get("detail", "") + " " + " ".join(facts)).strip()
        self._attach(f, opts, n)

    def _h_quota(self, f: dict, info: dict) -> None:
        ns, name = info["ns"], info["name"]
        q = next((x for x in self.quotas.get(ns, ()) if x["name"] == name), None)
        if not q:
            return
        tight = sorted(((q["used"].get(res, 0) / hard, res) for res, hard in q["hard"].items() if hard), reverse=True)
        tight = [(r, res) for r, res in tight if r >= 0.9][:3]
        if not tight:
            return
        raise_to = {res: (nice_cpu if _res_kind(res) == "cpu" else nice_mem if _res_kind(res) == "memory" else math.ceil)(
            q["hard"][res] * 1.25) for _, res in tight}
        hard_json = ",".join(f'"{res}":"{_q(res, v, True)}"' for res, v in raise_to.items())
        opts: list[dict | None] = []
        for _, res in tight:
            kind = _res_kind(res)
            if kind in ("cpu", "memory"):
                opts.append(self._reclaim(kind, "Lim" if res.startswith("limits.") else "Req", ns=ns))
        opts.append(_opt(
            f"Raise quota {name} by a quarter: " + ", ".join(f"{_q(res, v)} {res}" for res, v in raise_to.items()),
            "Leaves room for a rollout's surge pod and the next scale-up. Project owners usually can't edit their "
            "own quota.",
            f"{self.cli} -n {ns} patch resourcequota {name} --type merge -p '{{\"spec\":{{\"hard\":{{{hard_json}}}}}}}'",
            admin=True))
        self._attach(f, opts)

    def _h_limitrange(self, f: dict, info: dict) -> None:
        wid, msg = info["wid"], info.get("message", "")
        n = self.nodes[wid]
        m = re.search(r"(minimum|maximum) (cpu|memory) usage per (\w+) is (\S+?),? but (limit|request) is (\S+?)\.?(?:$|\s|,)", msg)
        c = self._container(wid)
        if not m or not c:
            return
        bound, res, _, allowed, field, asked = m.groups()
        value = _parse(res, allowed)
        if value is None:
            return
        kw = {"lim" if field == "limit" else "req": {res: value}}
        self._attach(f, [
            _opt(f"Set {own(c['name'])} {res} {field} to {_q(res, value)}",
                 f"The project's LimitRange allows a {bound} of {allowed} per container and the pod asks for {asked}. "
                 + ("If it genuinely needs more, the LimitRange has to change instead." if bound == "maximum" else ""),
                 self._set_cmd(n, c["name"], **kw)),
            _opt("Or change the project's LimitRange", "Needed when the workload really requires the value it asks for.",
                 f"{self.cli} -n {n['namespace']} describe limitrange", admin=True)], n)

    def _h_hpa_stuck(self, f: dict, info: dict) -> None:
        wid = info.get("wid")
        n = self.nodes.get(wid or "")
        if not n:
            return
        bare = [c for c in self.wl.get(wid, {}).get("containers", []) if not c["cpuReq"]]
        opts: list[dict | None] = []
        for c in bare[:2]:
            pk = self._peak(wid, c["name"])
            req = nice_cpu(max((pk[0] if pk else 0) * 1.25, 0.1))
            opts.append(_opt(
                f"Give {c['name']} a CPU request ({q_cpu(req)})",
                "A CPU-based autoscaler measures usage as a percentage of the request. With no request there is "
                "nothing to divide by, so it can't compute a replica count. "
                + (f"Sized from {q_cpu(pk[0])} used {self._basis(pk[2])}." if pk else "A conservative starting value."),
                self._set_cmd(n, c["name"], req={"cpu": req}), note=self._room(n, c, cpu_req=req)))
        opts.append(_opt("Check that pod metrics are being served",
                         "If this prints an error instead of numbers, the metrics pipeline is down and no autoscaler "
                         "in the cluster can work.", f"{self.cli} -n {n['namespace']} top pods", admin=not bare))
        self._attach(f, opts, n)

    # ------------------------------------------------------------------ findings this module raises
    def _saturation(self) -> None:
        for wid, n in self.d.workloads():
            cpu = [(p["cpu"] / p["cpuLimit"], p) for p in n["pods"] if not p["terminal"] and p.get("cpu") is not None
                   and p.get("cpuLimit")]
            mem = [(p["mem"] / p["memLimit"], p) for p in n["pods"] if not p["terminal"] and p.get("mem") is not None
                   and p.get("memLimit")]
            top_c = max((r for r, _ in cpu), default=0.0)
            top_m = max((r for r, _ in mem), default=0.0)
            if top_c >= 0.75:
                self._throttled(wid, n, top_c, [p["name"] for r, p in cpu if r >= 0.75])
            if top_m >= 0.8 and wid not in self.oom:
                self._near_limit(wid, n, top_m, [p["name"] for r, p in mem if r >= 0.8])

    def _throttled(self, wid: str, n: dict, ratio: float, pods: list[str]) -> None:
        c = self._container(wid, None, by="cpu")
        if not c or not c["cpuLim"]:
            return
        ns, hot = n["namespace"], ratio >= 0.9
        _, cause, fix, cmds, _ = A.WORKLOAD["cpu"]
        f = self.d.add("saturation", f"cpu:{wid}",
                       f"CPU at {round(ratio * 100)}% of its limit" + (": throttled, which shows up as latency" if hot else ""),
                       subject=wid, severity="critical" if hot else "warning", pattern="wl:cpu", cause=cause, fix=fix,
                       pods=pods, covers=("cpu",),
                       commands=[self.d.cmd(x, ns=ns, name=n["name"], ref=self._ref(n)) for x in cmds])
        if not f:
            return
        f["weight"] = 0.7 if hot else 0.4
        pk = self._peak(wid, c["name"])
        seen = pk[0] if pk else ratio * c["cpuLim"]
        limit = c["cpuLim"]
        pinned = seen >= limit * 0.95
        new = nice_cpu(limit * 2 if pinned else max(seen * 1.4, limit * 1.25))
        new_req = nice_cpu(min(new, seen)) if seen > (c["cpuReq"] or 0) * 1.1 else None
        opts: list[dict | None] = [_opt(
            f"Raise {own(c['name'])} CPU limit from {q_cpu(limit)} to {q_cpu(new)}",
            (f"It used {q_cpu(seen)} {self._basis(pk[2]) if pk else 'right now'}, which is the limit itself: the "
             f"limit hides how much it really wants, so double it and see where usage settles."
             if pinned else f"It used {q_cpu(seen)} {self._basis(pk[2]) if pk else 'right now'}; {q_cpu(new)} leaves "
                            f"it about 40% of headroom.")
            + (f" The request moves to {q_cpu(new_req)} so the node isn't overpacked." if new_req else ""),
            self._set_cmd(n, c["name"], req={"cpu": new_req} if new_req else None, lim={"cpu": new}),
            note=self._room(n, c, cpu_req=new_req, cpu_lim=new))]
        hpa, reps = n.get("hpa"), n["replicas"].get("desired") or 0
        if n.get("subkind") in SCALABLE and reps:
            want = max(reps + 1, math.ceil(reps * ratio / 0.7))
            extra = want - reps
            room = self._room(n, c, extra_pods=extra)
            if hpa and hpa.get("max"):
                if (hpa.get("current") or 0) >= hpa["max"]:
                    opts.append(_opt(
                        f"Let the autoscaler go to {max(want, hpa['max'] + 1)} replicas (it is pinned at {hpa['max']})",
                        "Spreads the same work over more pods. Only helps if the work can be shared: a single hot "
                        "request or a leader-only job stays throttled.",
                        f"{self.cli} -n {ns} patch hpa {hpa.get('name') or n['name']} --type merge -p "
                        f"'{{\"spec\":{{\"maxReplicas\":{max(want, hpa['max'] + 1)}}}}}'", note=room))
            else:
                opts.append(_opt(
                    f"Run {want} replicas instead of {reps}",
                    f"Brings each pod down to roughly 70% of its limit if the load spreads evenly. Only helps if the "
                    f"work can be shared between pods.",
                    f"{self.cli} -n {ns} scale {self._ref(n)} --replicas={want}", note=room))
                opts.append(_opt(
                    "Or let it scale itself",
                    f"An autoscaler between {reps} and {max(want, reps * 2)} replicas at 70% CPU adds pods when the "
                    f"load arrives and removes them after." + ("" if c["cpuReq"] else " It needs a CPU request to work."),
                    f"{self.cli} -n {ns} autoscale {self._ref(n)} --min={reps} --max={max(want, reps * 2)} --cpu-percent=70",
                    note=room))
        self._attach(f, opts, n)

    def _near_limit(self, wid: str, n: dict, ratio: float, pods: list[str]) -> None:
        c = self._container(wid)
        if not c or not c["memLim"]:
            return
        ns, hot = n["namespace"], ratio >= 0.9
        _, cause, fix, cmds, _ = A.WORKLOAD["mem"]
        f = self.d.add("saturation", f"mem:{wid}",
                       f"Memory at {round(ratio * 100)}% of its limit" + (": the next step is an OOM kill" if hot else ""),
                       subject=wid, severity="critical" if hot else "warning", pattern="wl:mem", cause=cause, fix=fix,
                       pods=pods, covers=("mem",),
                       commands=[self.d.cmd(x, ns=ns, name=n["name"], ref=self._ref(n)) for x in cmds])
        if not f:
            return
        f["weight"] = 0.6 if hot else 0.3
        pk = self._peak(wid, c["name"])
        seen = pk[1] if pk else ratio * c["memLim"]
        limit = c["memLim"]
        new = nice_mem(max(seen * 1.3, limit * 1.25))
        req = c["memReq"]
        new_req = new if req and abs(req - limit) < MI else (min(new, nice_mem(seen)) if seen > (req or 0) * 1.1 else None)
        self._attach(f, [
            _opt(f"Raise {own(c['name'])} memory limit from {q_mem(limit)} to {q_mem(new)}",
                 f"It used {q_mem(seen)} {self._basis(pk[2]) if pk else 'right now'}; {q_mem(new)} leaves about 30% above "
                 f"that." + (f" The request moves to {q_mem(new_req)} so the scheduler reserves what it really uses."
                             if new_req else ""),
                 self._set_cmd(n, c["name"], req={"memory": new_req} if new_req else None, lim={"memory": new}),
                 note=self._room(n, c, mem_req=new_req, mem_lim=new)),
            self._runtime_hint(c),
            _opt("If it keeps rising, more memory only buys time",
                 "A service that levels off needed the room. One that keeps climbing has a leak or an unbounded "
                 "cache: find what grows.", f"{self.cli} -n {ns} top pods --containers | grep {n['name']}")], n)

    def _hpa_max(self) -> None:
        for h in self.snap.get("hpas") or []:
            sp, st = h.get("spec") or {}, h.get("status") or {}
            t = sp.get("scaleTargetRef") or {}
            ns, name = ns_of(h), name_of(h)
            wid = f"wl:{ns}/{t.get('kind')}/{t.get('name')}"
            n = self.nodes.get(wid)
            top, cur = sp.get("maxReplicas"), st.get("currentReplicas") or 0
            if not n or not top or cur < top:
                continue
            target = next((((m.get("resource") or {}).get("target") or {}).get("averageUtilization")
                           for m in sp.get("metrics") or [] if (m.get("resource") or {}).get("name") == "cpu"), None)
            now = next((((m.get("resource") or {}).get("current") or {}).get("averageUtilization")
                        for m in st.get("currentMetrics") or [] if (m.get("resource") or {}).get("name") == "cpu"), None)
            _, cause, fix, cmds, _ = A.WORKLOAD["hpa-max"]
            if target and now is not None and now <= target:
                cause = (f"The autoscaler is at its maximum of {top}, and CPU is at {now}% against a target of "
                         f"{target}%: it is holding, with nothing left for the next peak.")
            f = self.d.add("saturation", f"hpa-max:{wid}", f"Autoscaler pinned at its maximum ({top} replicas)",
                           subject=wid, severity="warning", pattern="wl:hpa-max", cause=cause, fix=fix,
                           covers=("hpa-max",), commands=[self.d.cmd(x, ns=ns, name=name, ref=self._ref(n)) for x in cmds],
                           detail=f"CPU {now}% of request, target {target}%." if target and now is not None else "")
            if not f:
                continue
            f["weight"] = 0.5
            c = self._container(wid, None, by="cpu")
            if not c:
                continue
            want = max(top + 1, math.ceil(cur * now / target * 1.2)) if target and now and now > target \
                else math.ceil(top * 1.5)
            pc, pm = self._pod_request(wid)
            can = self._capacity(pc, pm) if self.hosts and (pc or pm) else None
            opts: list[dict | None] = [_opt(
                f"Raise maxReplicas from {top} to {want}",
                (f"At {now}% against a {target}% target the autoscaler wants about {math.ceil(cur * now / target)} "
                 f"replicas; {want} adds a fifth on top. " if target and now and now > target else
                 "Gives the autoscaler half as much room again. ")
                + (f"Each pod reserves {q_cpu(pc)} CPU / {q_mem(pm)}; the nodes can take about {can} more."
                   if can is not None else ""),
                f"{self.cli} -n {ns} patch hpa {name} --type merge -p '{{\"spec\":{{\"maxReplicas\":{want}}}}}'",
                note=self._room(n, c, extra_pods=want - cur))]
            if c["cpuLim"]:
                pk = self._peak(wid, c["name"])
                if pk and pk[0] >= c["cpuLim"] * 0.75:
                    new = nice_cpu(c["cpuLim"] * 1.5)
                    new_req = nice_cpu((c["cpuReq"] or c["cpuLim"] / 2) * 1.5)
                    opts.append(_opt(
                        f"Make each pod bigger: CPU {q_cpu(c['cpuReq'])} / {q_cpu(c['cpuLim'])} → {q_cpu(new_req)} / {q_cpu(new)}",
                        "The pods are also near their own CPU limit. Bigger pods do more work each, and because the "
                        "autoscaler measures against the request, the same load reads as a lower percentage.",
                        self._set_cmd(n, c["name"], req={"cpu": new_req}, lim={"cpu": new}),
                        note=self._room(n, c, cpu_req=new_req, cpu_lim=new)))
            stuck = cur - (n["replicas"].get("ready") or 0)
            if stuck > 0:
                opts.insert(0, _opt(
                    f"First get the {stuck} replica{'s' if stuck != 1 else ''} it already asked for running",
                    "The autoscaler is at its maximum on paper, but not all of those pods are serving. More replicas "
                    "would only join the queue: see this workload's other problem."))
            if can is not None and can < want - cur:
                opts.append(self._add_capacity(f"More replicas need somewhere to run: the nodes can take about {can} "
                                               f"more pods of this size."))
            self._attach(f, opts, n)

    def _oversized(self) -> None:
        """Workloads reserving far more than they use: information, and where to look when capacity is short."""
        for wid, n in self.d.workloads():
            w = self.wl[wid]
            if not w["live"] or n.get("platform"):
                continue
            pods = w["live"]
            for c in w["containers"]:
                pk = self._peak(wid, c["name"])
                if not pk or pk[2] < self.min_history:
                    continue
                req, lim, parts, waste_c, waste_m = {}, {}, [], 0.0, 0.0
                if c["cpuReq"] and pk[0] > 0 and pk[0] < c["cpuReq"] * 0.3:
                    want = nice_cpu(pk[0] * 1.4)
                    if (c["cpuReq"] - want) * pods >= 1.0:
                        req["cpu"], waste_c = want, (c["cpuReq"] - want) * pods
                        parts.append(f"CPU: reserves {q_cpu(c['cpuReq'])} per pod, peaked at {q_cpu(pk[0])}")
                if c["memReq"] and pk[1] > 0 and pk[1] < c["memReq"] * 0.4:
                    want = nice_mem(pk[1] * 1.3)
                    if (c["memReq"] - want) * pods >= 2 * GI:
                        req["memory"], waste_m = want, (c["memReq"] - want) * pods
                        parts.append(f"memory: reserves {q_mem(c['memReq'])} per pod, peaked at {q_mem(pk[1])}")
                        if c["memLim"] and c["memLim"] < want * 1.25:
                            lim["memory"] = nice_mem(want * 1.5)
                if not req:
                    continue
                freed = " and ".join(x for x in (f"{q_cpu(waste_c)} CPU" if waste_c else "",
                                                 f"{q_mem(waste_m)} of memory" if waste_m else "") if x)
                f = self.d.add("sizing", f"over:{wid}:{c['name']}", f"Reserves far more than it uses ({freed} idle)",
                               subject=wid, severity="info", pattern="sizing:over",
                               detail=f"{pods} pod{'s' if pods != 1 else ''}; " + "; ".join(parts) + f" {span_text(pk[2])}.",
                               cause="Requests are what the scheduler sets aside on a node and what a project's quota "
                                     "counts, used or not. Capacity reserved here is capacity other workloads can't have.",
                               fix="Lower the requests toward real usage, after checking a busy period.")
                if f:
                    self._attach(f, [_opt(
                        f"Lower {own(c['name'])} requests to " + " / ".join(f"{_q(k, v)} {k}" for k, v in req.items()),
                        f"About 30-40% above what it used {self._basis(pk[2])}. Limits stay where they are, so the "
                        f"pods can still burst. Check a busy period (month end, batch windows) first.",
                        self._set_cmd(n, c["name"], req=req, lim=lim or None))], n)
                break

    # ------------------------------------------------------------------ findings raised after scoring
    def _evicted(self, f: dict) -> None:
        wid = f["subject"]
        n, c = self.nodes.get(wid), self._container(wid)
        if not n or not c:
            return
        pk = self._peak(wid, c["name"])
        opts: list[dict | None] = []
        if pk and pk[1] > (c["memReq"] or 0) * 1.1:
            req = nice_mem(pk[1] * 1.1)
            opts.append(_opt(
                f"Raise {own(c['name'])} memory request to {q_mem(req)}",
                f"It uses {q_mem(pk[1])} against a request of {q_mem(c['memReq']) if c['memReq'] else 'nothing'}. When a "
                f"node runs short, the kubelet evicts pods using more than they requested first, and the scheduler "
                f"packs nodes by requests, so an honest request both protects the pod and stops the overpacking.",
                self._set_cmd(n, c["name"], req={"memory": req}), note=self._room(n, c, mem_req=req)))
        if (n["replicas"].get("desired") or 0) == 1 and n.get("subkind") in SCALABLE:
            opts.append(_opt("Run a second replica", "With one pod, every eviction is an outage until the "
                             "replacement is ready.",
                             f"{self.cli} -n {n['namespace']} scale {self._ref(n)} --replicas=2",
                             note=self._room(n, c, extra_pods=1)))
        if f.get("causedByName"):
            opts.append(_opt(f"Fix the node first: {f['causedByName']}", "These pods are the victims. The node's own "
                             "entry names the pod that is using the memory."))
        self._attach(f, opts, n)

    def _host(self, f: dict, code: str) -> None:
        hid = f["subject"]
        h = self.nodes.get(hid)
        if not h or h.get("pseudo"):
            return
        if code in ("node-mem-req", "node-cpu-req"):
            res = "memory" if "mem" in code else "cpu"
            self._attach(f, [self._reclaim(res, "Req", host=hid),
                             self._add_capacity("If the reservations are honest, the node is simply full.")])
            return
        if code not in ("pressure:MemoryPressure", "node-mem", "node-cpu"):
            return
        res = "cpu" if code == "node-cpu" else "memory"
        if (hid, res) in self._host_done:            # MemoryPressure and "95% of memory used" are one story
            return
        self._host_done.add((hid, res))
        use_k, req_k, lim_k = ("cpu", "cpuRequest", "cpuLimit") if res == "cpu" else ("mem", "memRequest", "memLimit")
        rows = []
        for wid, p in self.on_host.get(hid, ()):
            if p.get(use_k) is None or p["phase"] != "Running":
                continue
            over = p[use_k] - (p.get(req_k) or 0)
            if over > (0.25 if res == "cpu" else 256 * MI):
                rows.append((over, wid, p))
        rows.sort(key=lambda r: -r[0])
        opts: list[dict | None] = []
        for over, wid, p in rows[:2]:
            n, c = self.nodes[wid], self._container(wid, None, by="cpu" if res == "cpu" else "mem")
            if not c:
                continue
            used, asked, cap = p[use_k], p.get(req_k), p.get(lim_k)
            new_req = (nice_cpu if res == "cpu" else nice_mem)(used * 1.1)
            new_lim = None if cap else (nice_cpu if res == "cpu" else nice_mem)(used * 1.3)
            opts.append(_opt(
                f"Make {n['name']} reserve what it uses: {res} request {_q(res, asked) if asked else 'none'} → {_q(res, new_req)}",
                f"Its pod here uses {_q(res, used)} against a request of {_q(res, asked) if asked else 'nothing'}"
                + ("" if cap else " and has no limit") + f". The scheduler placed it by the request, so the node holds "
                f"{_q(res, over)} more than it planned for. With an honest request the pod is put where there is room"
                + (f"; a limit of {_q(res, new_lim)} stops it taking the node down with it." if new_lim else "."),
                self._set_cmd(n, c["name"], req={res: new_req}, lim={res: new_lim} if new_lim else None),
                note=self._managed(n)))
        if res == "memory":
            bare = sorted({self.nodes[wid]["name"] for wid, p in self.on_host.get(hid, ())
                           if p["phase"] == "Running" and not p.get("memLimit")})
            if len(bare) > 1:
                opts.append(_opt(
                    f"Give the {len(bare)} workloads here that have no memory limit one",
                    ", ".join(bare[:6]) + ("…" if len(bare) > 6 else "") + ". A pod with a limit is killed alone when it "
                    "overruns; a pod without one takes memory from its neighbours until the kubelet evicts them."))
        opts.append(self._add_capacity("If every pod here is sized honestly, the workloads have outgrown the nodes."))
        self._attach(f, opts)


def _opt(title: str, detail: str = "", command: str = "", *, admin: bool = False, note: str = "") -> dict:
    return {"title": title, "detail": detail.strip(), "command": command or "", "admin": bool(admin), "note": note or ""}


def _parse(res: str, value) -> float | None:
    if value is None:
        return None
    kind = _res_kind(res)
    if kind == "cpu":
        return parse_cpu(value)
    if kind == "memory" or "storage" in res:
        return parse_mem(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return parse_mem(value)
