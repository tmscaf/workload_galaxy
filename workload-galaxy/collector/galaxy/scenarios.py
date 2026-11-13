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
                last_terminated: str | None = None) -> dict:
        ns, app = key.split("/")
        base = self.pods_of(key)[0] if self.pods_of(key) else None
        cont = copy.deepcopy(container or base["spec"]["containers"][0])
        if image:
            cont["image"] = image
        owner = owner or (base["metadata"]["ownerReferences"][0] if base else None)
        name = f"{owner['name'] if owner and owner['kind'] != 'StatefulSet' else app}-{suffix or _h(key + str(len(self.objs['pods'])))}"
        status: dict = {"phase": phase, "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
                        "containerStatuses": [{
                            "name": app, "restartCount": restarts,
                            "state": {"waiting": {"reason": waiting, "message": message}} if waiting else {"running": {}},
                            "lastState": {"terminated": {"reason": last_terminated, "finishedAt": _iso(90)}}
                            if last_terminated else {}}]}
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
    old = w.pods_of(key)[0]
    rs_name = f"cart-{_h('cart-rc1', 9)}"
    w.objs["replicasets"].append({"metadata": {"name": rs_name, "namespace": "storefront",
                                               "ownerReferences": [{"kind": "Deployment", "name": "cart",
                                                                    "controller": True}]},
                                  "spec": {"replicas": 1}})
    w.add_pod(key, node=old["spec"]["nodeName"], phase="Pending", ready=False, waiting="ImagePullBackOff",
              message='Back-off pulling image "quay.io/acme/cart:1.6.0-rc1"', image="quay.io/acme/cart:1.6.0-rc1",
              owner={"kind": "ReplicaSet", "name": rs_name, "controller": True}, age=420)
    return {
        "focus": "wl:storefront/Deployment/cart",
        "layers": {
            "hosts": "Unaffected.",
            "pods": "One red hex: the new cart pod can't pull quay.io/acme/cart:1.6.0-rc1. The old pods keep serving.",
            "services": "cart lists two images and '1 pod in ImagePullBackOff'. The rollout is stuck, but 2/2 old "
                        "replicas are ready, so traffic is unaffected.",
            "users": "No impact, unless someone scales the old ReplicaSet down.",
        },
    }


def db_outage(w: World) -> dict:
    key = "data/orders-db"
    for p in w.pods_of(key):
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
]}
