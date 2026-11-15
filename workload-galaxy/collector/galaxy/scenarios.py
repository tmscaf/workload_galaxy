"""Fault scenarios for the demo cluster.

Each scenario mutates a per-refresh copy of the demo cluster (a `World`):
nodes, pods, metrics, agent socket reports and mesh metrics. The result is fed
through the normal builder, so what you see is exactly what the detector would
report on a real cluster in that state. Scenarios can be combined.

Every scenario returns a guide: what to look for on each strata layer, plus the
node to focus.
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

GI = 1024  # Mi per Gi


def _iso(seconds_ago: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds").replace("+00:00", "Z")


def _h(text: str, n: int = 5) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:n]


def _uid(text: str) -> str:
    h = hashlib.sha1(text.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _mi(q: str | None) -> float:
    if not q:
        return 0.0
    q = str(q)
    if q.endswith("Gi"):
        return float(q[:-2]) * GI
    if q.endswith("Mi"):
        return float(q[:-2])
    return float(q) / (1024 * 1024)


def _cores(q: str | None) -> float:
    if not q:
        return 0.0
    q = str(q)
    return float(q[:-1]) / 1000 if q.endswith("m") else float(q)


class World:
    """Mutable copy of the demo cluster for one refresh."""

    def __init__(self, src) -> None:
        self.src = src
        self.objs = copy.deepcopy(src.objs)
        self.usage: dict[str, tuple[float, float]] = dict(src.usage)   # "ns/pod" -> (cores, Mi)
        self.dead_nodes: set[str] = set()          # kubelet + agent silent
        self.net_degraded: dict[str, float] = {}   # node -> RTT multiplier (with retransmits)
        self.slow_nodes: dict[str, float] = {}     # node -> request latency multiplier (contention)
        self.mesh_err: dict[str, float] = {}       # "ns/name" callee -> extra 5xx rate
        self.mesh_slow: dict[str, float] = {}      # "ns/name" callee -> latency multiplier
        self.blocked: set[tuple[str, str]] = set()  # (caller or "*", callee): connections never get through
        self.guides: dict[str, dict] = {}
        self._ip = 180

    # ------------------------------------------------------------ lookups
    def node(self, name: str) -> dict:
        return next(n for n in self.objs["nodes"] if n["metadata"]["name"] == name)

    def node_names(self) -> list[str]:
        return [n["metadata"]["name"] for n in self.objs["nodes"]]

    @staticmethod
    def key_of(pod: dict) -> str:
        return f"{pod['metadata']['namespace']}/{pod['metadata']['labels'].get('app')}"

    def pods_of(self, key: str) -> list[dict]:
        ns, app = key.split("/")
        return [p for p in self.objs["pods"] if p["metadata"]["namespace"] == ns
                and p["metadata"]["labels"].get("app") == app]

    def pods_on(self, node: str) -> list[dict]:
        return [p for p in self.objs["pods"] if p["spec"].get("nodeName") == node
                and p["status"].get("phase") not in ("Failed", "Succeeded")]

    def controller(self, key: str) -> tuple[str, dict] | None:
        ns, name = key.split("/")
        for plural in ("deployments", "statefulsets", "daemonsets", "jobs"):
            for o in self.objs[plural]:
                if o["metadata"]["namespace"] == ns and o["metadata"]["name"] == name:
                    return plural, o
        return None

    def requested_mi(self, node: str) -> float:
        return sum(_mi(c["resources"].get("requests", {}).get("memory"))
                   for p in self.pods_on(node) for c in p["spec"]["containers"])

    def used_cores(self, node: str) -> float:
        return sum(self.usage.get(f"{p['metadata']['namespace']}/{p['metadata']['name']}", (0, 0))[0]
                   for p in self.pods_on(node))

    def alloc_mi(self, node: str) -> float:
        return _mi(self.node(node)["status"]["allocatable"]["memory"])

    def alloc_cores(self, node: str) -> float:
        return _cores(self.node(node)["status"]["allocatable"]["cpu"])

    def workloads_on(self, node: str) -> list[str]:
        return sorted({self.key_of(p) for p in self.pods_on(node)})

    def singletons_on(self, node: str) -> list[str]:
        out = []
        for key in self.workloads_on(node):
            live = [p for p in self.pods_of(key) if p["status"].get("phase") not in ("Failed", "Succeeded")]
            if all(p["spec"].get("nodeName") == node for p in live):
                out.append(key)
        return out

    # ------------------------------------------------------------ node mutations
    def set_condition(self, node: str, ctype: str, status: str, reason: str = "", message: str = "") -> None:
        conds = self.node(node)["status"]["conditions"]
        for c in conds:
            if c["type"] == ctype:
                c.update(status=status, reason=reason, message=message)
                return
        conds.append({"type": ctype, "status": status, "reason": reason, "message": message})

    def taint(self, node: str, key: str, effect: str, value: str | None = None) -> None:
        t = {"key": key, "effect": effect}
        if value:
            t["value"] = value
        self.node(node)["spec"].setdefault("taints", []).append(t)

    # ------------------------------------------------------------ pod mutations
    def _next_ip(self, node: str | None) -> str:
        self._ip += 1
        idx = int(node[-1]) if node else 0
        return f"10.128.{idx * 2 + 1}.{self._ip}"

    def set_ready(self, pod: dict, ready: bool) -> None:
        for c in pod["status"]["conditions"]:
            if c["type"] == "Ready":
                c["status"] = "True" if ready else "False"

    def evict(self, pod: dict, message: str) -> None:
        pod["status"] = {"phase": "Failed", "reason": "Evicted", "message": message,
                         "conditions": [{"type": "Ready", "status": "False"}], "containerStatuses": []}
        self.usage.pop(f"{pod['metadata']['namespace']}/{pod['metadata']['name']}", None)

    def add_pod(self, key: str, *, node: str | None, phase: str = "Running", ready: bool = True,
                waiting: str | None = None, message: str = "", age: float = 60, suffix: str | None = None,
                owner: dict | None = None, image: str | None = None, container: dict | None = None,
                usage: tuple[float, float] | None = None, restarts: int = 0,
                last_terminated: str | None = None, exit_code: int | None = None) -> dict:
        ns, app = key.split("/")
        base = self.pods_of(key)[0] if self.pods_of(key) else None
        cont = copy.deepcopy(container or base["spec"]["containers"][0])
        if image:
            cont["image"] = image
        owner = owner or (base["metadata"]["ownerReferences"][0] if base else None)
        name = f"{owner['name'] if owner and owner['kind'] != 'StatefulSet' else app}-{suffix or _h(key + str(len(self.objs['pods'])))}"
        term = {"reason": last_terminated, "finishedAt": _iso(90)}
        if exit_code is not None:
            term["exitCode"] = exit_code
        status: dict = {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False",
                                                        "lastTransitionTime": _iso(age)}],
                        "containerStatuses": [{
                            "name": app, "restartCount": restarts,
                            "state": {"waiting": {"reason": waiting, "message": message}} if waiting else {"running": {}},
                            "lastState": {"terminated": term} if last_terminated else {}}]}
        if node and phase == "Running":
            ip = self._next_ip(node)
            status.update(podIP=ip, podIPs=[{"ip": ip}])
        if not node:
            status["conditions"].append({"type": "PodScheduled", "status": "False", "reason": "Unschedulable",
                                         "message": message})
        pod = {"metadata": {"name": name, "namespace": ns, "uid": _uid(f"{name}/{len(self.objs['pods'])}"),
                            "labels": {"app": app}, "ownerReferences": [owner] if owner else [],
                            "creationTimestamp": _iso(age)},
               "spec": {"nodeName": node or "", "containers": [cont]}, "status": status}
        self.objs["pods"].append(pod)
        if usage and node and phase == "Running":
            self.usage[f"{ns}/{name}"] = usage
        return pod

    def add_workload(self, ns: str, name: str, kind: str, *, cpu_req: float, mem_req_mi: float,
                     cpu_limit: float | None, mem_limit_mi: float | None, image: str) -> dict:
        labels = {"app": name}
        resources: dict = {"requests": {"cpu": f"{int(cpu_req * 1000)}m", "memory": f"{int(mem_req_mi)}Mi"}}
        limits = {}
        if cpu_limit:
            limits["cpu"] = f"{int(cpu_limit * 1000)}m"
        if mem_limit_mi:
            limits["memory"] = f"{int(mem_limit_mi)}Mi"
        if limits:
            resources["limits"] = limits
        container = {"name": name, "image": image, "resources": resources, "env": []}
        obj = {"metadata": {"name": name, "namespace": ns, "uid": _uid(f"{ns}/{name}"), "labels": labels},
               "spec": {"replicas": 1, "selector": {"matchLabels": labels},
                        "template": {"metadata": {"labels": labels}, "spec": {"containers": [container]}}},
               "status": {"active": 1} if kind == "Job" else {}}
        plural = {"Deployment": "deployments", "StatefulSet": "statefulsets", "Job": "jobs"}[kind]
        self.objs[plural].append(obj)
        owner = {"kind": kind, "name": name, "controller": True}
        if kind == "Deployment":
            rs = f"{name}-{_h(name, 9)}"
            self.objs["replicasets"].append({"metadata": {"name": rs, "namespace": ns, "ownerReferences": [owner]},
                                             "spec": {"replicas": 1}})
            owner = {"kind": "ReplicaSet", "name": rs, "controller": True}
        obj["_owner"] = owner
        obj["_container"] = container
        return obj

    def event(self, kind: str, ns: str, name: str, reason: str, message: str, *, age: float = 60, count: int = 1) -> None:
        self.objs["events"].append({"type": "Warning", "reason": reason, "message": message, "count": count,
                                    "lastTimestamp": _iso(age),
                                    "involvedObject": {"kind": kind, "namespace": ns, "name": name}})

    def rollout(self, key: str, *, age: float, image: str | None = None, mutate=None, pods: int = 1,
                **pod_kw) -> tuple[dict, list[dict]]:
        """Start a new revision of a Deployment: a new ReplicaSet with the changed template, plus `pods` new pods."""
        ns, name = key.split("/")
        dep = self.controller(key)[1]
        owned = [r for r in self.objs["replicasets"] if r["metadata"]["namespace"] == ns
                 and r["metadata"]["ownerReferences"][0]["name"] == name]
        cur = max(int((r["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision") or 1)
                  for r in owned)
        tmpl = copy.deepcopy(dep["spec"]["template"])
        if image:
            tmpl["spec"]["containers"][0]["image"] = image
        if mutate:
            mutate(tmpl["spec"]["containers"][0])
        dep["spec"]["template"] = tmpl
        rs_name = f"{name}-{_h(key + str(cur + 1), 9)}"
        self.objs["replicasets"].append({
            "metadata": {"name": rs_name, "namespace": ns, "creationTimestamp": _iso(age),
                         "annotations": {"deployment.kubernetes.io/revision": str(cur + 1)},
                         "ownerReferences": [{"kind": "Deployment", "name": name, "controller": True}]},
            "spec": {"replicas": pods, "template": tmpl}})
        owner = {"kind": "ReplicaSet", "name": rs_name, "controller": True}
        node = self.pods_of(key)[0]["spec"]["nodeName"]
        new = [self.add_pod(key, node=pod_kw.pop("node", node), owner=owner, container=tmpl["spec"]["containers"][0],
                            age=max(5, age - 8), **pod_kw) for _ in range(pods)]
        return owner, new

    def scale(self, key: str, replicas: int) -> None:
        found = self.controller(key)
        if found:
            found[1]["spec"]["replicas"] = replicas


# ============================================================================ scenarios
def memory_pressure(w: World) -> dict:
    node = "ocp-worker-2"
    w.set_condition(node, "MemoryPressure", "True", "KubeletHasInsufficientMemory",
                    "kubelet has insufficient memory available")
    w.taint(node, "node.kubernetes.io/memory-pressure", "NoSchedule")
    victims = [p for p in w.pods_on(node) if p["metadata"]["ownerReferences"][0]["kind"] == "ReplicaSet"][:2]
    used = sum(w.usage.get(f"{p['metadata']['namespace']}/{p['metadata']['name']}", (0, 0))[1]
               for p in w.pods_on(node) if p not in victims)
    hog_mem = w.alloc_mi(node) * 0.95 - used
    etl = w.add_workload("data", "etl-backfill", "Deployment", cpu_req=0.5, mem_req_mi=2 * GI,
                         cpu_limit=None, mem_limit_mi=None, image="quay.io/acme/etl-backfill:0.9.2")
    w.add_pod("data/etl-backfill", node=node, owner=etl["_owner"], container=etl["_container"],
              usage=(1.4, hog_mem), age=1500)
    others = [n for n in w.node_names() if n != node]
    moved = []
    for i, v in enumerate(victims):
        key = w.key_of(v)
        moved.append(key.split("/")[1])
        w.evict(v, "The node was low on resource: memory. Threshold quantity: 100Mi, available: 64Mi. "
                   "Container etl-backfill was using 26Gi, request is 2Gi.")
        w.add_pod(key, node=others[i % len(others)], phase="Pending", ready=False, waiting="ContainerCreating",
                  age=35, owner=v["metadata"]["ownerReferences"][0])
    return {
        "focus": f"host:{node}",
        "layers": {
            "hosts": f"{node} turns red: MemoryPressure, pods using ~95% of its memory. The kubelet is evicting pods "
                     f"and the node is tainted so nothing new lands there.",
            "pods": f"Grey hexes on {node} are the evicted pods ({', '.join(moved)}), still on record. Amber hexes on "
                    f"other nodes are their replacements starting. etl-backfill (no memory limit) is the pod "
                    f"eating the memory.",
            "services": f"{', '.join(moved)} drop below their desired replicas and turn amber. Their dashed halo "
                        f"and drawer point at {node} as the root cause.",
            "users": "Requests through the affected services lose capacity while the replacements start.",
        },
    }


def memory_reservations(w: World) -> dict:
    node = "ocp-worker-1"
    base = w.requested_mi(node)
    req = int(w.alloc_mi(node) * 0.97 - base)
    wh = w.add_workload("data", "analytics-warehouse", "StatefulSet", cpu_req=2, mem_req_mi=req,
                        cpu_limit=4, mem_limit_mi=req * 1.8, image="quay.io/acme/warehouse:3.2.1")
    w.add_pod("data/analytics-warehouse", node=node, owner=wh["_owner"], container=wh["_container"],
              usage=(1.1, req * 0.55), suffix="0", age=86400 * 3)
    used = sum(w.usage.get(f"{p['metadata']['namespace']}/{p['metadata']['name']}", (0, 0))[1]
               for p in w.pods_on(node)) / w.alloc_mi(node)
    free = max(w.alloc_mi(n) - w.requested_mi(n) for n in w.node_names())
    job_req = int(free + 2 * GI)
    job = w.add_workload("data", "search-reindex", "Job", cpu_req=2, mem_req_mi=job_req, cpu_limit=None,
                         mem_limit_mi=job_req, image="quay.io/acme/search-reindex:1.4.0")
    w.add_pod("data/search-reindex", node=None, phase="Pending", ready=False, owner=job["_owner"],
              container=job["_container"], age=600,
              message=f"0/3 nodes are available: 3 Insufficient memory. preemption: 0/3 nodes are available: "
                      f"3 No preemption victims found for incoming pod.")
    return {
        "focus": f"host:{node}",
        "layers": {
            "hosts": f"{node}'s reservation gauge is full: memory requests reserve 97% of allocatable while ~"
                     f"{round(used * 100)}% is actually used (the white line), and limits are overcommitted. "
                     f"The red dashed 'Unscheduled' box holds "
                     f"search-reindex, which asks for {job_req / GI:.0f} GiB. Every node has less than that free "
                     f"by reservation, not by usage.",
            "pods": "search-reindex's pod is a red hex hanging off the Unscheduled box. It has no node.",
            "services": "search-reindex shows 'can't be scheduled: Insufficient memory'. analytics-warehouse looks "
                        "healthy; it is the reservation, not the usage, that blocks others.",
            "users": "No user-facing impact yet. This is lost headroom: the next reschedule or scale-up fails.",
        },
    }


def unschedulable(w: World) -> dict:
    w.scale("storefront/catalog", 5)
    for i in range(2):
        w.add_pod("storefront/catalog", node=None, phase="Pending", ready=False, age=240 + i * 30,
                  message="0/3 nodes are available: 3 node(s) didn't match Pod's node affinity/selector. "
                          "preemption: 0/3 nodes are available: 3 Preemption is not helpful for scheduling.")
    w.objs["hpas"].append({"metadata": {"name": "catalog", "namespace": "storefront"},
                           "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "catalog"},
                                    "minReplicas": 3, "maxReplicas": 5},
                           "status": {"currentReplicas": 5, "desiredReplicas": 5}})
    w.taint("ocp-worker-1", "dedicated", "NoSchedule", "infra")
    w.scale("data/search-index", 4)
    w.add_pod("data/search-index", node=None, phase="Pending", ready=False, suffix="3", age=900,
              owner={"kind": "StatefulSet", "name": "search-index", "controller": True},
              message="0/3 nodes are available: 1 node(s) had untolerated taint {dedicated: infra}, "
                      "2 node(s) didn't match pod anti-affinity rules.")
    w.scale("data/order-worker", 4)
    for i in range(2):
        pod = w.add_pod("data/order-worker", node=None, phase="Pending", ready=False, age=120,
                        message="0/3 nodes are available: 3 Insufficient cpu.")
        pod["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "6"
    return {
        "focus": "host:(unscheduled)",
        "layers": {
            "hosts": "A red dashed 'Unscheduled' box appears beside the hosts. It is the scheduler's queue. "
                     "ocp-worker-1 now carries a dedicated=infra taint.",
            "pods": "Five red hexes hang off the Unscheduled box. Hover them for the scheduler's reason: node "
                    "selector, taint plus anti-affinity, insufficient CPU.",
            "services": "catalog is at 3/5 (its autoscaler asked for 5), search-index at 3/4 and order-worker at 2/4. "
                        "Each shows 'can't be scheduled' with the exact reason.",
            "users": "Requests still succeed, but the capacity the autoscaler wanted never arrived.",
        },
    }


def node_not_ready(w: World) -> dict:
    node = "ocp-worker-3"
    w.set_condition(node, "Ready", "Unknown", "NodeStatusUnknown", "Kubelet stopped posting node status.")
    w.taint(node, "node.kubernetes.io/unreachable", "NoExecute")
    w.taint(node, "node.kubernetes.io/unreachable", "NoSchedule")
    singles = w.singletons_on(node)
    affected = w.workloads_on(node)
    for p in w.pods_on(node):
        w.set_ready(p, False)
        w.usage.pop(f"{p['metadata']['namespace']}/{p['metadata']['name']}", None)
    w.dead_nodes.add(node)
    names = [k.split("/")[1] for k in singles]
    return {
        "focus": f"host:{node}",
        "layers": {
            "hosts": f"{node} turns red: unreachable, because the kubelet stopped posting status. Its flow agent "
                     f"goes silent as well.",
            "pods": f"Every hex on {node} turns red. The pods are still listed as Running but are no longer Ready.",
            "services": f"{len(affected)} workloads had a replica there and lose capacity."
                        + (f" {', '.join(names)} had their only replica there and go critical." if names else "")
                        + f" Their callers inherit impact pointing at {node}.",
            "users": "Paths through the lost workloads fail or time out.",
        },
    }


def noisy_neighbour(w: World) -> dict:
    node = "ocp-worker-1"
    hog = max(0.5, w.alloc_cores(node) * 0.97 - w.used_cores(node))
    job = w.add_workload("data", "nightly-export", "Job", cpu_req=0.1, mem_req_mi=512, cpu_limit=None,
                         mem_limit_mi=2 * GI, image="quay.io/acme/nightly-export:2.0.0")
    w.add_pod("data/nightly-export", node=node, owner=job["_owner"], container=job["_container"],
              usage=(hog, 1400), age=1800)
    w.slow_nodes[node] = 2.6
    victims = [k.split("/")[1] for k in w.workloads_on(node) if not k.endswith("nightly-export")][:4]
    return {
        "focus": f"host:{node}",
        "layers": {
            "hosts": f"{node} turns red: its pods use ~97% of the node's CPU.",
            "pods": f"nightly-export's hex is flagged 'Noisy neighbour: using {hog:.1f} cores with no CPU limit'.",
            "services": f"Unrelated workloads with replicas on {node} (e.g. {', '.join(victims)}) get slower p95. "
                        f"Their impact points at {node}, not at each other.",
            "users": "Request paths that cross those workloads slow down.",
        },
    }


def node_network(w: World) -> dict:
    node = "ocp-worker-2"
    w.net_degraded[node] = 14.0
    w.slow_nodes[node] = 3.0
    return {
        "focus": f"host:{node}",
        "layers": {
            "hosts": f"{node} turns red: retransmits on many connections from its pods and RTT ~10x the cluster "
                     f"median. This is the root cause.",
            "pods": "The pods themselves look healthy. The problem is below them.",
            "services": f"Calls to and from workloads with pods on {node} turn amber/red one by one. On its own that "
                        f"looks like many unrelated problems, but their impact all points at {node}.",
            "users": "Latency rises across many paths at once.",
        },
    }


def oom_loop(w: World) -> dict:
    key = "storefront/recommendations"
    pods = w.pods_of(key)
    for i, p in enumerate(pods):
        cs = p["status"]["containerStatuses"][0]
        cs["restartCount"] = 14 + i * 3
        cs["lastState"] = {"terminated": {"reason": "OOMKilled", "exitCode": 137, "finishedAt": _iso(70 + i * 40)}}
        ns_pod = f"{p['metadata']['namespace']}/{p['metadata']['name']}"
        if i == 0:
            cs["state"] = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 2m40s restarting failed container"}}
            w.set_ready(p, False)
            w.usage.pop(ns_pod, None)
        else:
            cpu, _ = w.usage.get(ns_pod, (0.5, 0))
            w.usage[ns_pod] = (cpu, 1004)
    w.mesh_err[key] = 0.25
    w.mesh_slow[key] = 3.0
    return {
        "focus": "wl:storefront/Deployment/recommendations",
        "layers": {
            "hosts": "Unaffected. The hosts stay green; compare this with memory pressure, where the node is the "
                     "problem.",
            "pods": "recommendations' hexes go red and amber: OOMKilled, CrashLoopBackOff, memory at 98% of its limit.",
            "services": "recommendations is critical; api-gateway → recommendations shows ~25% 5xx.",
            "users": "Some storefront requests fail.",
        },
    }


def bad_rollout(w: World) -> dict:
    key = "storefront/cart"
    image = "quay.io/acme/cart:1.6.0-rc1"
    _owner, new = w.rollout(key, age=420, image=image, phase="Pending", ready=False, waiting="ImagePullBackOff",
                            message=f'Back-off pulling image "{image}"')
    w.event("Pod", "storefront", new[0]["metadata"]["name"], "Failed", count=6, age=40,
            message=f'Failed to pull image "{image}": rpc error: code = NotFound desc = failed to pull and unpack '
                    f'image "{image}": failed to resolve reference "{image}": manifest unknown')
    return {
        "focus": "wl:storefront/Deployment/cart",
        "layers": {
            "hosts": "Unaffected.",
            "pods": "One red hex: the new cart pod can't pull quay.io/acme/cart:1.6.0-rc1. The old pods keep serving.",
            "services": "cart lists two images and '1 pod in ImagePullBackOff'. The rollout is stuck, but 2/2 old "
                        "replicas are ready, so traffic is unaffected.",
            "users": "No impact, unless someone scales the old ReplicaSet down.",
        },
        "triage": "Rollout stuck (what changed: the image tag) and, as its cause, 'can't pull cart:1.6.0-rc1: the "
                  "image or tag does not exist'.",
    }


def db_outage(w: World) -> dict:
    key = "data/orders-db"
    for p in (x for x in w.pods_of(key) if x["status"].get("containerStatuses")):
        cs = p["status"]["containerStatuses"][0]
        cs["restartCount"] = 9
        cs["state"] = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m0s restarting failed container"}}
        cs["lastState"] = {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": _iso(50)}}
        w.set_ready(p, False)
        w.usage.pop(f"{p['metadata']['namespace']}/{p['metadata']['name']}", None)
    callers = sorted({src for src, dst, *_ in w.src.calls() if dst == key})
    for c in callers:
        w.mesh_err[c] = 0.35
        w.mesh_slow[c] = 6.0
    return {
        "focus": "wl:data/StatefulSet/orders-db",
        "layers": {
            "hosts": "Healthy. A reminder that red at the top doesn't mean red at the bottom.",
            "pods": "orders-db-0 is red (CrashLoopBackOff). The callers' pods look fine.",
            "services": f"orders-db is critical at 0/1 ready. {', '.join(c.split('/')[1] for c in callers)} show "
                        f"unanswered connects, and everything upstream inherits impact pointing at orders-db.",
            "users": "The riskiest request paths now end at orders-db: shopping, payments and login all degrade.",
        },
    }


def disk_pressure(w: World) -> dict:
    node = "ocp-worker-3"
    w.set_condition(node, "DiskPressure", "True", "KubeletHasDiskPressure", "kubelet has disk pressure")
    w.taint(node, "node.kubernetes.io/disk-pressure", "NoSchedule")
    victim = next((p for p in w.pods_on(node) if p["metadata"]["ownerReferences"][0]["kind"] == "ReplicaSet"), None)
    name = ""
    if victim:
        key = w.key_of(victim)
        name = key.split("/")[1]
        w.evict(victim, "The node was low on resource: ephemeral-storage. Threshold quantity: 10%, available: 6%. "
                        f"Container {name} was using 14Gi, request is 0.")
        other = next(n for n in w.node_names() if n != node)
        w.add_pod(key, node=other, phase="Pending", ready=False, waiting="ContainerCreating", age=25,
                  owner=victim["metadata"]["ownerReferences"][0])
    return {
        "focus": f"host:{node}",
        "layers": {
            "hosts": f"{node} turns red: DiskPressure. The kubelet garbage-collects images and evicts pods, and "
                     f"the node is tainted NoSchedule.",
            "pods": f"A grey hex is the evicted {name} pod (it filled its ephemeral storage). An amber replacement "
                    f"is starting elsewhere.",
            "services": f"{name} runs one replica short until the replacement is ready."
                        if name else "Workloads on the node are at risk of eviction.",
            "users": "Minor capacity loss.",
        },
    }


# --------------------------------------------------------------------------- deployment failures
def _guide(focus: str, triage: str, services: str, pods: str = "", users: str = "", hosts: str = "Unaffected.") -> dict:
    return {"focus": focus, "triage": triage,
            "layers": {"hosts": hosts, "pods": pods, "services": services, "users": users}}


def missing_config(w: World) -> dict:
    key = "storefront/recommendations"
    w.objs["configmaps"].append({"metadata": {"name": "recommendation-settings", "namespace": "storefront"},
                                 "data": {"MODEL": "v4"}})

    def change(c: dict) -> None:
        c["envFrom"] = [{"configMapRef": {"name": "recommendations-settings"}}]
    w.rollout(key, age=300, image="quay.io/acme/recommendations:1.16.0", mutate=change, phase="Pending", ready=False,
              waiting="CreateContainerConfigError", message='configmap "recommendations-settings" not found')
    return _guide("wl:storefront/Deployment/recommendations",
                  "Rollout stuck, caused by: ConfigMap recommendations-settings is missing - with the hint that a "
                  "ConfigMap named recommendation-settings exists (a typo).",
                  "recommendations: rollout stuck, old pods still serving.", "The new pod is red: CreateContainerConfigError.",
                  "No impact while the old pods run.")


def crash_on_start(w: World) -> dict:
    key = "identity/user-profile"
    for p in (x for x in w.pods_of(key) if x["status"].get("containerStatuses")):
        cs = p["status"]["containerStatuses"][0]
        cs["restartCount"] = 9
        cs["state"] = {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m0s restarting failed container"}}
        cs["lastState"] = {"terminated": {"reason": "Error", "exitCode": 127, "finishedAt": _iso(110),
                                          "message": "exec: \"/app/start-server.sh\": stat /app/start-server.sh: "
                                                     "no such file or directory"}}
        w.set_ready(p, False)
        w.usage.pop(f"{p['metadata']['namespace']}/{p['metadata']['name']}", None)
        w.event("Pod", "identity", p["metadata"]["name"], "BackOff", "Back-off restarting failed container user-profile",
                age=30, count=41)
    return _guide("wl:identity/Deployment/user-profile",
                  "2 pods crash-looping: command not found (exit 127), with the container's own last message. auth "
                  "→ user-profile failures are listed under it as symptoms.",
                  "user-profile is down (0/2); auth, which calls it, degrades.", "Both user-profile pods are red.",
                  "Logins that need the profile fail.")


def quota_exceeded(w: World) -> dict:
    key = "payments/payments-api"
    w.scale(key, 4)
    q = w.objs["resourcequotas"][0]
    q["status"]["used"] = {"limits.memory": "7936Mi", "limits.cpu": "6500m", "pods": "6"}
    dep = w.controller(key)[1]
    msg = ('pods "payments-api-7c9d8-" is forbidden: exceeded quota: compute, requested: limits.memory=512Mi, '
           'used: limits.memory=7936Mi, limited: limits.memory=8Gi')
    dep["status"] = {"conditions": [{"type": "ReplicaFailure", "status": "True", "reason": "FailedCreate", "message": msg}]}
    rs = next(r for r in w.objs["replicasets"] if r["metadata"]["ownerReferences"][0]["name"] == "payments-api")
    w.event("ReplicaSet", "payments", rs["metadata"]["name"], "FailedCreate", "Error creating: " + msg, age=20, count=14)
    return _guide("wl:payments/Deployment/payments-api",
                  "Quota: 'Can't create pods: the project quota is exhausted', with the API server's message "
                  "(requested / used / limit) and the quota itself at 97%.",
                  "payments-api shows 2/4 ready: the scale-up never produced pods.",
                  "No new pods appear at all - nothing to see on the pods plane, which is why this one is hard to spot.",
                  "Payments run at half the intended capacity.")


def scc_denied(w: World) -> dict:
    key = "data/order-worker"

    def change(c: dict) -> None:
        c["securityContext"] = {"runAsUser": 0}
    w.rollout(key, age=900, image="quay.io/acme/order-worker:1.13.0", mutate=change, pods=0)
    rs = w.objs["replicasets"][-1]
    msg = ('pods "order-worker-" is forbidden: unable to validate against any security context constraint: '
           '[provider "anyuid": Forbidden: not usable by user or serviceaccount, provider restricted-v2: '
           '.containers[0].runAsUser: Invalid value: 0: must be in the ranges: [1000680000, 1000689999]]')
    w.event("ReplicaSet", "data", rs["metadata"]["name"], "FailedCreate", "Error creating: " + msg, age=25, count=22)
    return _guide("wl:data/Deployment/order-worker",
                  "Admission: 'Can't create pods: the pod is rejected by the security policy' (SCC), plus the stuck "
                  "rollout and what changed in it.",
                  "order-worker: rollout stuck at 0 new pods; the old version keeps running.",
                  "No new pod exists, so the pods plane looks healthy.", "No impact yet.")


def pvc_pending(w: World) -> dict:
    key = "data/search-index"
    w.scale(key, 4)
    claim = "data-search-index-3"
    w.objs["persistentvolumeclaims"].append({
        "metadata": {"name": claim, "namespace": "data", "creationTimestamp": _iso(780)},
        "spec": {"storageClassName": "fast-ssd", "accessModes": ["ReadWriteOnce"],
                 "resources": {"requests": {"storage": "50Gi"}}}, "status": {"phase": "Pending"}})
    w.event("PersistentVolumeClaim", "data", claim, "ProvisioningFailed",
            'storageclass.storage.k8s.io "fast-ssd" not found', age=35, count=52)
    msg = "0/3 nodes are available: pod has unbound immediate PersistentVolumeClaims. preemption: 0/3 nodes are available"
    pod = w.add_pod(key, node=None, phase="Pending", ready=False, message=msg, age=780, suffix="3",
                    owner={"kind": "StatefulSet", "name": "search-index", "controller": True})
    pod["spec"]["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]
    return _guide("wl:data/StatefulSet/search-index",
                  "Storage: 'Volume claim data-search-index-3 is not bound', cause 'StorageClass \"fast-ssd\" does not "
                  "exist'. The unschedulable pod is explained by it.",
                  "search-index shows 3/4 ready.", "search-index-3 hangs off the Unscheduled queue.",
                  "None: three replicas still serve.", "The Unscheduled queue holds one pod.")


def probe_failing(w: World) -> dict:
    key = "identity/auth"

    def change(c: dict) -> None:
        c["readinessProbe"] = {"httpGet": {"path": "/healthz/ready", "port": 8080}, "periodSeconds": 5}
    _owner, new = w.rollout(key, age=840, image="quay.io/acme/auth:1.5.0", mutate=change, ready=False,
                            usage=(0.08, 140))
    w.event("Pod", "identity", new[0]["metadata"]["name"], "Unhealthy",
            "Readiness probe failed: HTTP probe failed with statuscode: 404", age=4, count=160)
    dep = w.controller(key)[1]
    dep["status"] = {"conditions": [{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded",
                                     "message": 'ReplicaSet "auth" has timed out progressing.'}]}
    return _guide("wl:identity/Deployment/auth",
                  "Rollout failed (ProgressDeadlineExceeded) with what changed (image, readiness probe), and Probes: "
                  "'running but failing the readiness probe: the probe path doesn't exist in the app (HTTP 404)'.",
                  "auth: rollout failed; 2/2 old pods serve.", "The new auth pod is amber: Running, never Ready.",
                  "No impact: unready pods get no traffic.")


def liveness_kill(w: World) -> dict:
    key = "storefront/web-frontend"
    for i, p in enumerate(x for x in w.pods_of(key) if x["status"].get("containerStatuses")):
        cs = p["status"]["containerStatuses"][0]
        cs["restartCount"] = 6 + i
        cs["lastState"] = {"terminated": {"reason": "Error", "exitCode": 137, "finishedAt": _iso(50 + 70 * i)}}
        w.event("Pod", "storefront", p["metadata"]["name"], "Unhealthy", age=20 + 30 * i, count=30,
                message="Liveness probe failed: Get \"http://10.128.2.14:8080/live\": context deadline exceeded "
                        "(Client.Timeout exceeded while awaiting headers)")
    w.mesh_err[key] = 0.08
    w.mesh_slow[key] = 2.5
    return _guide("wl:storefront/Deployment/web-frontend",
                  "Probes: '3 pods restarted by a failing liveness probe' - the app doesn't answer within the probe "
                  "timeout, so the kubelet keeps killing it. Without the events this looks like a plain crash (exit 137).",
                  "web-frontend restarts repeatedly; requests in flight fail.", "Its pods show recent restarts.",
                  "Intermittent errors on the shop.")


def port_mismatch(w: World) -> dict:
    key = "storefront/catalog"
    svc = next(x for x in w.objs["services"] if x["metadata"]["name"] == "catalog")
    svc["spec"]["ports"][0]["targetPort"] = 8081
    w.blocked.add(("*", key))
    return _guide("wl:storefront/Deployment/catalog",
                  "Network: 'Service catalog sends traffic to port 8081, but the pods don't listen there' (they "
                  "listen on 8080, seen by the node agent). Every caller's failure is a symptom of it.",
                  "catalog's pods are all green, yet every call to it fails.", "All catalog pods are Ready.",
                  "The shop can't show products.")


def netpol_block(w: World) -> dict:
    w.objs["networkpolicies"].append({
        "metadata": {"name": "allow-from-data-tier", "namespace": "data"},
        "spec": {"podSelector": {"matchLabels": {"app": "orders-db"}}, "policyTypes": ["Ingress"],
                 "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"tier": "data"}}}],
                              "ports": [{"protocol": "TCP", "port": 5432}]}]}})
    for src in ("storefront/catalog", "payments/ledger", "identity/user-profile"):
        w.blocked.add((src, "data/orders-db"))
    return _guide("wl:data/StatefulSet/orders-db",
                  "Network: 'NetworkPolicy blocks catalog → orders-db' (and ledger, user-profile): policy "
                  "allow-from-data-tier only admits namespaces labelled tier=data. order-worker, in the data "
                  "namespace, still gets through.",
                  "orders-db itself is healthy; three callers from other projects can't connect.",
                  "Pods are all Ready on both sides.", "Catalog, payments and profile requests time out.")


def route_broken(w: World) -> dict:
    w.objs["routes"].append({"metadata": {"name": "checkout", "namespace": "storefront", "creationTimestamp": _iso(600)},
                             "spec": {"host": "checkout.acme.example", "to": {"kind": "Service", "name": "cart-svc"},
                                      "tls": {"termination": "edge"}},
                             "status": {"ingress": [{"host": "checkout.acme.example", "conditions": [
                                 {"type": "Admitted", "status": "True"}]}]}})
    w.objs["routes"].append({"metadata": {"name": "promo", "namespace": "storefront", "creationTimestamp": _iso(600)},
                             "spec": {"host": "shop.acme.example", "path": "/promo",
                                      "to": {"kind": "Service", "name": "web-frontend"}},
                             "status": {"ingress": [{"host": "shop.acme.example", "conditions": [
                                 {"type": "Admitted", "status": "False", "reason": "HostAlreadyClaimed",
                                  "message": "route shop already exposes shop.acme.example and is older"}]}]}})
    return _guide("entry:storefront/Route/checkout",
                  "Network: 'Route checkout points at Service cart-svc, which doesn't exist' (with the hint that "
                  "'cart' does) and 'Route promo is not admitted: HostAlreadyClaimed'.",
                  "Two new routes are red; everything behind them is healthy.", "Unaffected.",
                  "checkout.acme.example returns 503.")


def failed_job(w: World) -> dict:
    owner = {"kind": "CronJob", "name": "nightly-export", "controller": True}
    w.objs["cronjobs"].append({"metadata": {"name": "nightly-export", "namespace": "data"},
                               "spec": {"schedule": "0 2 * * *", "jobTemplate": {"spec": {"template": {"spec": {
                                   "containers": [{"name": "export", "image": "quay.io/acme/export:2.1.0",
                                                   "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                                                                 "limits": {"memory": "256Mi"}}}]}}}}},
                               "status": {"lastScheduleTime": _iso(5400)}})
    w.objs["jobs"].append({"metadata": {"name": "nightly-export-29384221", "namespace": "data", "ownerReferences": [owner],
                                        "creationTimestamp": _iso(5400)},
                           "spec": {"backoffLimit": 3},
                           "status": {"failed": 4, "conditions": [{
                               "type": "Failed", "status": "True", "reason": "BackoffLimitExceeded",
                               "message": "Job has reached the specified backoff limit",
                               "lastTransitionTime": _iso(4900)}]}})
    return _guide("wl:data/CronJob/nightly-export",
                  "Jobs: 'CronJob nightly-export: last run failed (BackoffLimitExceeded)'. Failed batch pods are "
                  "history, so the maps don't show them; the Triage view does.",
                  "nightly-export appears as an idle CronJob.", "No pods (the failed ones are finished).", "None.")


def operator_degraded(w: World) -> dict:
    for co in w.objs["clusteroperators"]:
        if co["metadata"]["name"] == "ingress":
            co["status"]["conditions"] = [
                {"type": "Available", "status": "True"}, {"type": "Progressing", "status": "True"},
                {"type": "Degraded", "status": "True", "reason": "IngressDegraded",
                 "message": "The \"default\" ingress controller reports Degraded=True: DeploymentReplicasNotAvailable: "
                            "1/2 of replicas are available"}]
    mcp = w.objs["machineconfigpools"][0]
    mcp["status"].update(updatedMachineCount=1, conditions=[{"type": "Updating", "status": "True"},
                                                            {"type": "Degraded", "status": "False"}])
    w.node("ocp-worker-2")["spec"]["unschedulable"] = True
    for router in w.pods_of("openshift-ingress/router-default")[-1:]:
        w.set_ready(router, False)
    return _guide("host:ocp-worker-2",
                  "Platform strip turns amber: ingress operator degraded, worker pool updating (1/3), one node "
                  "cordoned. These show whatever project filter is set, because they explain symptoms inside projects.",
                  "router-default runs at 1/2.", "One router pod is unready.", "Ingress capacity is halved.",
                  "ocp-worker-2 is cordoned for the update.")


# Curated combinations for showing what the board can do: each tells one kind of story.
PRESETS = [
    {"id": "deploy-failures", "title": "A bad release day",
     "summary": "Six deployments fail six different ways: bad image tag, missing ConfigMap, crash on start, "
                "exhausted quota, security policy, failing readiness probe.",
     "scenarios": ["bad-rollout", "missing-config", "crash-on-start", "quota-exceeded", "scc-denied", "probe-failing"]},
    {"id": "network", "title": "Wiring and network faults",
     "summary": "Pods are green but calls fail: wrong Service port, a NetworkPolicy cutting callers off, broken "
                "routes, packet loss on one node.",
     "scenarios": ["port-mismatch", "netpol-block", "route-broken", "node-network"]},
    {"id": "infrastructure", "title": "Infrastructure trouble",
     "summary": "A node goes NotReady, another runs out of memory, pods can't be scheduled, a volume won't "
                "provision, and the platform is mid-update.",
     "scenarios": ["node-not-ready", "memory-pressure", "unschedulable", "pvc-pending", "operator-degraded"]},
    {"id": "cascade", "title": "One root cause, many victims",
     "summary": "The orders database goes down and takes its callers, and their callers, with it.",
     "scenarios": ["db-outage", "liveness-kill", "failed-job"]},
]


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    summary: str
    layers: tuple[str, ...]      # which strata layers show the root cause
    apply: Callable[[World], dict]


SCENARIOS: dict[str, Scenario] = {s.id: s for s in [
    Scenario("memory-pressure", "Node memory pressure + evictions",
             "A pod with no memory limit eats ocp-worker-2; the kubelet evicts others.", ("hosts", "pods"),
             memory_pressure),
    Scenario("memory-reservations", "Memory reservations exhausted",
             "Requests reserve ~97% of ocp-worker-1 while usage is low; a big job fits nowhere.", ("hosts",),
             memory_reservations),
    Scenario("unschedulable", "Pods can't be scheduled",
             "Scale-ups blocked by node selectors, taints, anti-affinity and CPU requests.", ("hosts", "pods"),
             unschedulable),
    Scenario("node-not-ready", "Node NotReady (kubelet lost)",
             "ocp-worker-3 stops reporting; everything on it goes unready.", ("hosts",), node_not_ready),
    Scenario("noisy-neighbour", "CPU noisy neighbour",
             "An unlimited batch job saturates ocp-worker-1's CPU and slows its neighbours.", ("hosts", "pods"),
             noisy_neighbour),
    Scenario("node-network", "Packet loss on one node",
             "ocp-worker-2's NIC drops packets: retransmits and RTT spikes for its pods.", ("hosts",), node_network),
    Scenario("disk-pressure", "Node disk pressure",
             "ocp-worker-3 runs out of ephemeral storage and evicts a pod.", ("hosts", "pods"), disk_pressure),
    Scenario("oom-loop", "OOMKill loop",
             "recommendations' memory limit is too small; pods OOM and crash-loop.", ("pods", "services"), oom_loop),
    Scenario("bad-rollout", "Stuck rollout (ImagePullBackOff)",
             "cart's new version can't pull its image; old pods keep serving.", ("pods",), bad_rollout),
    Scenario("db-outage", "Database outage cascade",
             "orders-db crash-loops; every caller and their callers degrade.", ("services", "users"), db_outage),
    Scenario("missing-config", "Deploy fails: missing ConfigMap",
             "recommendations' new version references a ConfigMap whose name has a typo.", ("pods",), missing_config),
    Scenario("crash-on-start", "Deploy fails: crash on start",
             "user-profile exits with code 127 (command not found) and crash-loops.", ("pods", "services"), crash_on_start),
    Scenario("quota-exceeded", "Deploy fails: project quota",
             "payments-api can't scale: the project's memory quota is exhausted.", ("services",), quota_exceeded),
    Scenario("scc-denied", "Deploy fails: security policy (SCC)",
             "order-worker's new version asks to run as root; no SCC admits it.", ("services",), scc_denied),
    Scenario("pvc-pending", "Deploy fails: volume not provisioned",
             "search-index scales up, but its claim names a StorageClass that doesn't exist.", ("pods", "hosts"), pvc_pending),
    Scenario("probe-failing", "Deploy fails: readiness probe",
             "auth's new version probes a path that returns 404; the rollout times out.", ("pods",), probe_failing),
    Scenario("liveness-kill", "Liveness probe kills the app",
             "web-frontend is too slow to answer its liveness probe and is restarted over and over.", ("pods", "services"),
             liveness_kill),
    Scenario("port-mismatch", "Service points at the wrong port",
             "catalog's Service targets 8081; the pods listen on 8080. Pods green, every call fails.", ("services",),
             port_mismatch),
    Scenario("netpol-block", "NetworkPolicy blocks callers",
             "A new policy on orders-db only admits the data tier; three callers are cut off.", ("services",), netpol_block),
    Scenario("route-broken", "Route misconfigured",
             "One route points at a Service that doesn't exist; another claims a hostname already in use.", ("users",),
             route_broken),
    Scenario("failed-job", "CronJob run failed",
             "nightly-export hit its backoff limit last night.", ("services",), failed_job),
    Scenario("operator-degraded", "Platform: operator degraded + node update",
             "The ingress operator is degraded while the worker pool is being updated.", ("hosts",), operator_degraded),
]}
