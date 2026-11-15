"""Build the workload graph from a raw cluster snapshot plus telemetry.

Graph model (all edges point in request direction, upstream -> downstream):

  nodes  clients    the outside world (end users, LAN clients)
         entry      Ingress / OpenShift Route / Gateway HTTPRoute
         workload   Deployment, StatefulSet, DaemonSet, DeploymentConfig, CronJob, Job, bare Pod
                    (pods are embedded in their workload; Services are folded into edges)
         service    only for Services with no backends - calls into them go nowhere
         external   dependencies outside the cluster (hostnames, IPs, the Kubernetes API)
         host       Kubernetes nodes the pods run on
  edges  exposes    clients -> entry / workload (LoadBalancer, NodePort, ingress controller)
         route      entry -> workload, ingress controller -> entry
         calls      workload -> workload / external / service

Every edge carries the evidence it is based on: declared (API objects),
config (env / ConfigMap references), observed (agent sockets / conntrack),
mesh (Prometheus request metrics) and probe (active connect checks).
"""
from __future__ import annotations

import ipaddress
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from . import inference
from .diagnose import Diagnoser
from .flows import FlowStats, Observations, aggregate
from .probes import ProbeResult
from .prom import MeshEdge
from .risk import score_graph
from .util import (controller_ref, ip_is_local, is_ip, iso_now, labels_of, meta, name_of, normalize_ip, now_utc,
                   ns_of, parse_cpu, parse_mem, parse_ts, percentile)

PLATFORM_NS = re.compile(
    r"^(kube-system|kube-public|kube-node-lease|default|local|openshift|openshift-.*|kube-.*|cattle-.*|fleet-.*|"
    r"cluster-fleet-.*|p-[a-z0-9]+|user-[a-z0-9]+|local-p-[a-z0-9]+|u-[a-z0-9]+|cert-manager|istio-system|"
    r"calico-.*|tigera-.*|metallb-system|longhorn-system|ingress-nginx|traefik|monitoring|gatekeeper-system)$"
)
INGRESS_CONTROLLER = re.compile(r"traefik|ingress|nginx|haproxy|envoy|contour|kong|router|gateway|istio-ingress", re.I)
K3S_SVCLB_LABEL = "svccontroller.k3s.cattle.io/svcname"
UNSCHEDULED = "host:(unscheduled)"
# Your own network: RFC 1918, shared address space (CGNAT), link-local, loopback, IPv6 ULA/link-local.
# Extend with GALAXY_INTERNAL_CIDRS. (Documentation ranges count as public, so demo data looks real.)
INTERNAL_NETS = [ipaddress.ip_network(c) for c in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16", "127.0.0.0/8",
    "fc00::/7", "fe80::/10", "::1/128")]
INTERNAL_SUFFIX = re.compile(r"\.(local|lan|internal|intranet|corp|home\.arpa|localdomain|private)$")
MAX_EXTERNAL_NODES = 25
MAX_DETAILS = 8


# --------------------------------------------------------------------------- accumulators
@dataclass
class EdgeAcc:
    evidence: set[str] = field(default_factory=set)
    details: list[str] = field(default_factory=list)
    via: set[str] = field(default_factory=set)
    ports: set[str] = field(default_factory=set)
    flow: FlowStats | None = None
    mesh: MeshEdge | None = None
    probe: ProbeResult | None = None
    etype: str = "calls"

    def add_detail(self, text: str) -> None:
        if text and text not in self.details and len(self.details) < MAX_DETAILS:
            self.details.append(text)

    def add_flow(self, stats: FlowStats) -> None:
        if self.flow is None:
            self.flow = FlowStats()
        self.flow.merge(stats)
        self.evidence.add("observed")
        self.ports |= stats.ports

    def merge(self, other: "EdgeAcc") -> None:
        self.evidence |= other.evidence
        for d in other.details:
            self.add_detail(d)
        self.via |= other.via
        self.ports |= other.ports
        if other.flow:
            self.add_flow(other.flow)
        if other.mesh and not self.mesh:
            self.mesh = other.mesh
        if other.probe and not self.probe:
            self.probe = other.probe
        if _ETYPE_RANK[other.etype] > _ETYPE_RANK[self.etype]:
            self.etype = other.etype


_ETYPE_RANK = {"calls": 0, "route": 1, "exposes": 2}


def _short_image(image: str) -> str:
    image = image.split("@", 1)[0]
    return image.rsplit("/", 1)[-1]


def _container_info(cs: dict, image: str, init: bool, now) -> dict:
    """One container's state, as a troubleshooter needs it: what it is doing and how it last died."""
    state = cs.get("state") or {}
    kind = next((k for k in ("running", "waiting", "terminated") if state.get(k) is not None), "unknown")
    cur = state.get(kind) or {}
    last = (cs.get("lastState") or {}).get("terminated") or (cur if kind == "terminated" else None)
    out = {"name": cs.get("name", ""), "image": image, "init": init, "state": kind,
           "reason": cur.get("reason", ""), "message": (cur.get("message") or "")[:400],
           "ready": bool(cs.get("ready")), "restarts": int(cs.get("restartCount") or 0), "last": None}
    if kind == "terminated":
        out["exitCode"] = cur.get("exitCode")
    if last:
        finished = parse_ts(last.get("finishedAt"))
        out["last"] = {"reason": last.get("reason", ""), "exitCode": last.get("exitCode"), "signal": last.get("signal"),
                       "message": (last.get("message") or "")[:400],
                       "ageS": int((now - finished).total_seconds()) if finished else None}
    return out


# --------------------------------------------------------------------------- builder
class Builder:
    def __init__(self, snap: dict, *, reports: list[dict] | None = None,
                 mesh: dict[tuple[str, str, str, str], MeshEdge] | None = None,
                 probes: tuple[dict, dict] | None = None, ext_dns: dict[str, str] | None = None,
                 hidden_namespaces: set[str] | None = None, sources: dict | None = None,
                 flow_idle_seconds: float = 60, internal_cidrs: list[str] | None = None,
                 event_window_seconds: float = 3600):
        self.snap = snap
        self.reports = reports or []
        self.mesh = mesh or {}
        self.vip_probes, self.ep_probes = probes or ({}, {})
        self.ext_dns = ext_dns or {}
        self.hidden = hidden_namespaces or set()
        self.sources = sources or {}
        self.flow_idle_seconds = flow_idle_seconds
        self.event_window_seconds = event_window_seconds
        self.internal_nets = list(INTERNAL_NETS)
        for cidr in internal_cidrs or []:
            try:
                self.internal_nets.append(ipaddress.ip_network(cidr, strict=False))
            except ValueError:
                pass
        self.nodes: dict[str, dict] = {}
        self.svc_edges: dict[tuple[str, str], EdgeAcc] = defaultdict(EdgeAcc)   # (src node, svc id)
        self.edges: dict[tuple[str, str], EdgeAcc] = defaultdict(EdgeAcc)       # (src node, dst node)
        self.external_hosts: set[str] = set()

    # ---------------------------------------------------------------- public
    def build(self) -> dict:
        self._index()
        self._hosts()
        self._workloads()
        self._services()
        self._endpoint_probes()
        self._entries()
        self._declared_edges()
        self._config_edges()
        self._observed_edges()
        self._host_network()
        self._mesh_edges()
        self._collapse_services()
        self._fold_ingress_controllers()
        self._cap_externals()
        self._classify_externals()
        edges = self._finalize_edges()
        self._drop_hidden(edges)
        diag = Diagnoser(self, edges, event_window_seconds=self.event_window_seconds)
        diag.run()
        graph = {
            "generatedAt": iso_now(),
            "cluster": {"name": self.snap.get("clusterName", ""), "openshift": bool(self.snap.get("openshift")),
                        "nodes": len(self.snap.get("nodes", []))},
            "sources": self.sources,
            "nodes": list(self.nodes.values()),
            "edges": edges,
        }
        score_graph(graph, listen=self._listen, node_probe=self._node_probe)
        diag.finish(graph)
        self._quota_ratio = diag.quota_ratio
        graph["namespaces"] = self._namespace_summary(graph)
        graph["externalHosts"] = sorted(self.external_hosts)
        return graph

    # ---------------------------------------------------------------- indexes
    def _index(self) -> None:
        s = self.snap
        self.rs = {(ns_of(o), name_of(o)): o for o in s.get("replicasets", [])}
        self.rc = {(ns_of(o), name_of(o)): o for o in s.get("replicationcontrollers", [])}
        self.jobs = {(ns_of(o), name_of(o)): o for o in s.get("jobs", [])}
        self.controllers: dict[tuple[str, str, str], dict] = {}
        for key, kind in (("deployments", "Deployment"), ("statefulsets", "StatefulSet"), ("daemonsets", "DaemonSet"),
                          ("deploymentconfigs", "DeploymentConfig"), ("cronjobs", "CronJob"), ("jobs", "Job"),
                          ("replicasets", "ReplicaSet")):
            for o in s.get(key, []):
                self.controllers[(ns_of(o), kind, name_of(o))] = o
        self.metrics = {}
        for m in s.get("podmetrics", []):
            cpu = sum(parse_cpu((c.get("usage") or {}).get("cpu")) or 0 for c in m.get("containers") or [])
            mem = sum(parse_mem((c.get("usage") or {}).get("memory")) or 0 for c in m.get("containers") or [])
            self.metrics[(ns_of(m), name_of(m))] = (cpu, mem)
        self.pods_by_key: dict[str, dict] = {}
        self.pod_wl: dict[str, str] = {}
        self.pod_uid: dict[str, str] = {}
        self.pod_ip: dict[str, str] = {}
        self.svclb_pod: dict[str, str] = {}
        self._listen: dict[str, dict] = {}
        self._node_probe: dict[str, dict] = {}

    def _workload_key(self, pod: dict) -> tuple[str, str]:
        ref = controller_ref(pod)
        ns = ns_of(pod)
        if not ref or ref.get("kind") == "Node":
            return "Pod", name_of(pod)
        kind, name = ref.get("kind", ""), ref.get("name", "")
        if kind == "ReplicaSet":
            rs = self.rs.get((ns, name))
            owner = controller_ref(rs) if rs else None
            if owner and owner.get("kind") == "Deployment":
                return "Deployment", owner["name"]
            if rs is None and labels_of(pod).get("pod-template-hash") and name.endswith(
                    "-" + labels_of(pod)["pod-template-hash"]):
                return "Deployment", name.rsplit("-", 1)[0]
            return "ReplicaSet", name
        if kind == "ReplicationController":
            rc = self.rc.get((ns, name))
            owner = controller_ref(rc) if rc else None
            if owner and owner.get("kind") == "DeploymentConfig":
                return "DeploymentConfig", owner["name"]
            dc = (meta(pod).get("annotations") or {}).get("openshift.io/deployment-config.name")
            if dc:
                return "DeploymentConfig", dc
            return "ReplicationController", name
        if kind == "Job":
            job = self.jobs.get((ns, name))
            owner = controller_ref(job) if job else None
            if owner and owner.get("kind") == "CronJob":
                return "CronJob", owner["name"]
            return "Job", name
        return kind, name

    # ---------------------------------------------------------------- hosts
    def _hosts(self) -> None:
        self.node_ips: dict[str, str] = {}
        self.gateway_ips: dict[str, str] = {}
        for n in self.snap.get("nodes", []):
            name = name_of(n)
            status = n.get("status") or {}
            for addr in status.get("addresses") or []:
                if addr.get("type") in ("InternalIP", "ExternalIP"):
                    self.node_ips[addr["address"]] = name
            for cidr in list((n.get("spec") or {}).get("podCIDRs") or []) or [(n.get("spec") or {}).get("podCIDR")]:
                if not cidr:
                    continue
                try:
                    net = ipaddress.ip_network(cidr, strict=False)
                    self.gateway_ips[str(net.network_address + 1)] = name
                except ValueError:
                    pass
            conds = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
            labels = labels_of(n)
            roles = sorted(k.split("/", 1)[1] for k in labels if k.startswith("node-role.kubernetes.io/"))
            alloc = status.get("allocatable") or {}
            cap = status.get("capacity") or {}
            pressure = [k for k in ("MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable")
                        if conds.get(k) == "True"]
            ready_cond = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), {})
            taints = [f"{t.get('key')}{'=' + t['value'] if t.get('value') else ''}:{t.get('effect')}"
                      for t in (n.get("spec") or {}).get("taints") or []]
            self.nodes[f"host:{name}"] = {
                "id": f"host:{name}", "kind": "host", "subkind": "Node", "name": name, "namespace": "",
                "ready": conds.get("Ready") == "True", "readyStatus": conds.get("Ready", "Unknown"),
                "readyMessage": ready_cond.get("message", "") if conds.get("Ready") != "True" else "",
                "pressure": pressure, "taints": taints,
                "zone": labels.get("topology.kubernetes.io/zone", ""), "roles": roles or ["worker"],
                "cpuAllocatable": parse_cpu(alloc.get("cpu")), "memAllocatable": parse_mem(alloc.get("memory")),
                "cpuCapacity": parse_cpu(cap.get("cpu")), "memCapacity": parse_mem(cap.get("memory")),
                "cpuUsed": 0.0, "memUsed": 0.0, "podCount": 0,
                # Scheduler's view: what pods have reserved (requests) and may grow to (limits).
                "cpuRequested": 0.0, "memRequested": 0.0, "memLimits": 0.0, "podsWithoutMemLimit": 0,
                "kubelet": (status.get("nodeInfo") or {}).get("kubeletVersion", ""),
                "os": (status.get("nodeInfo") or {}).get("osImage", ""),
                "unschedulable": bool((n.get("spec") or {}).get("unschedulable")),
                "net": None, "agentStale": False,
            }

    def _unscheduled_host(self) -> dict:
        if UNSCHEDULED not in self.nodes:
            self.nodes[UNSCHEDULED] = {
                "id": UNSCHEDULED, "kind": "host", "subkind": "Unscheduled", "name": "Unscheduled", "namespace": "",
                "pseudo": True, "ready": True, "readyStatus": "n/a", "pressure": [], "taints": [], "zone": "",
                "roles": ["scheduler queue"], "podCount": 0, "pending": [], "cpuUsed": 0.0, "memUsed": 0.0,
                "cpuAllocatable": None, "memAllocatable": None, "net": None, "agentStale": False,
            }
        return self.nodes[UNSCHEDULED]

    def _host_network(self) -> None:
        """Per-node TCP health from the agent: sockets opened by pods on each node."""
        stale = set((self.sources.get("agent") or {}).get("stale") or [])
        for host in self.nodes.values():
            if host["kind"] == "host" and not host.get("pseudo"):
                host["agentStale"] = host["name"] in stale
        # A slow *destination* makes every caller's RTT high; a bad *node* makes the same calls slower
        # from it than from other nodes. So compare each connection with the cluster-wide median RTT
        # to the same destination, and blame a node only when it accounts for most abnormal connections.
        conns: list[tuple[str, tuple, float | None, bool]] = []
        for report in self.reports:
            hid = f"host:{report.get('node')}"
            if hid not in self.nodes:
                continue
            for netns in report.get("netns") or []:
                for s in netns.get("sockets") or []:
                    if s.get("dir") != "out" or s.get("state") != "ESTAB":
                        continue
                    peer = self._resolve(s.get("rip", ""), s.get("rport"), "tcp")
                    if not peer or peer[0] == "ext":
                        continue   # internet RTT says nothing about this node's network
                    rtt = float(s["rtt"]) if s.get("rtt") is not None else None
                    conns.append((hid, peer, rtt, bool(s.get("retrans"))))
        by_peer: dict[tuple, list[float]] = defaultdict(list)
        for _, peer, rtt, _ in conns:
            if rtt is not None:
                by_peer[peer].append(rtt)
        median = {peer: percentile(v, 0.5) for peer, v in by_peer.items()}
        hosts = {c[0] for c in conns}
        stats: dict[str, dict] = {h: {"conns": 0, "retrans": 0, "slow": 0, "rtts": []} for h in hosts}
        for hid, peer, rtt, retrans in conns:
            st = stats[hid]
            st["conns"] += 1
            st["retrans"] += retrans
            if rtt is not None:
                st["rtts"].append(rtt)
                med = median.get(peer) or 0
                if len(by_peer[peer]) >= 3 and rtt > 1.0 and rtt > 3 * med:
                    st["slow"] += 1
        total_retrans = sum(s["retrans"] for s in stats.values())
        total_slow = sum(s["slow"] for s in stats.values())
        all_rtts = [r for s in stats.values() for r in s["rtts"]]
        cluster_p50 = percentile(all_rtts, 0.5)
        for hid, st in stats.items():
            if not st["conns"]:
                continue
            self.nodes[hid]["net"] = {
                "connections": st["conns"], "retransConnections": st["retrans"], "slowConnections": st["slow"],
                # Share of the cluster's abnormal connections that originate on this node.
                "retransShare": round(st["retrans"] / total_retrans, 3) if total_retrans else 0.0,
                "slowShare": round(st["slow"] / total_slow, 3) if total_slow else 0.0,
                "reportingNodes": len(stats),
                "rttP50Ms": round(percentile(st["rtts"], 0.5), 3) if st["rtts"] else None,
                "rttP95Ms": round(percentile(st["rtts"], 0.95), 3) if st["rtts"] else None,
                "clusterRttP50Ms": round(cluster_p50, 3) if cluster_p50 is not None else None,
            }

    # ---------------------------------------------------------------- workloads & pods
    def _new_workload(self, ns: str, kind: str, name: str) -> dict:
        wid = f"wl:{ns}/{kind}/{name}"
        if wid not in self.nodes:
            self.nodes[wid] = {
                "id": wid, "kind": "workload", "subkind": kind, "name": name, "namespace": ns,
                "platform": bool(PLATFORM_NS.match(ns)), "replicas": {"desired": 0, "ready": 0, "current": 0},
                "pods": [], "services": [], "images": [], "hosts": [], "template": None,
                "hpa": None, "completed": False, "labels": {}, "diag": [],
            }
        return self.nodes[wid]

    def _workloads(self) -> None:
        now = now_utc()
        for pod in self.snap.get("pods", []):
            ns, pname = ns_of(pod), name_of(pod)
            spec, status = pod.get("spec") or {}, pod.get("status") or {}
            phase = status.get("phase", "Unknown")
            kind, wname = self._workload_key(pod)
            if phase in ("Succeeded",) or (phase == "Failed" and kind in ("Job", "CronJob")):
                continue  # finished batch pods are history, not topology
            key = f"{ns}/{pname}"
            lb_svc = labels_of(pod).get(K3S_SVCLB_LABEL)
            if lb_svc:
                # k3s ServiceLB proxy pods are plumbing of a LoadBalancer Service, not a workload.
                lb_ns = labels_of(pod).get("svccontroller.k3s.cattle.io/svcnamespace", "")
                self.svclb_pod[key] = f"svc:{lb_ns}/{lb_svc}" if lb_ns else lb_svc
                for ip in self._pod_ips(pod):
                    self.pod_ip[ip] = key
                continue
            wl = self._new_workload(ns, kind, wname)
            wid = wl["id"]
            if wl["template"] is None:
                wl["template"] = spec
            cstats = status.get("containerStatuses") or []
            istats = status.get("initContainerStatuses") or []
            restarts = sum(int(c.get("restartCount") or 0) for c in cstats + istats)
            recent = 0
            reason = ""
            # An init container that is failing or still running is the pod's real state (as kubectl shows it).
            for i, c in enumerate(istats):
                st = c.get("state") or {}
                term, wait = st.get("terminated"), st.get("waiting")
                if term and int(term.get("exitCode") or 0) == 0:
                    continue
                if term:
                    reason = f"Init:{term.get('reason') or 'Error'}"
                elif wait and wait.get("reason") and wait["reason"] != "PodInitializing":
                    reason = f"Init:{wait['reason']}"
                elif phase == "Pending":
                    reason = f"Init:{i}/{len(istats)}"
                break
            for c in cstats:
                term = ((c.get("lastState") or {}).get("terminated") or {})
                finished = parse_ts(term.get("finishedAt"))
                if finished and (now - finished).total_seconds() < 3600:
                    recent += 1
                waiting = ((c.get("state") or {}).get("waiting") or {}).get("reason")
                if waiting and not reason:
                    reason = waiting
                if not reason and term.get("reason") == "OOMKilled" and finished and \
                        (now - finished).total_seconds() < 3600:
                    reason = "OOMKilled"
            if not reason and status.get("reason"):
                reason = status["reason"]
            conds = {c.get("type"): c.get("status") for c in status.get("conditions") or []}
            ready = conds.get("Ready") == "True"
            sched = next((c for c in status.get("conditions") or []
                          if c.get("type") == "PodScheduled" and c.get("status") == "False"), None)
            message = status.get("message") or (sched or {}).get("message", "")
            if phase == "Pending" and not reason:
                reason = (sched or {}).get("reason") or "Pending"
            containers = spec.get("containers") or []

            def total(field_name: str, res: str, parser) -> float | None:
                vals = [parser(((c.get("resources") or {}).get(field_name) or {}).get(res)) for c in containers]
                return None if not vals or any(v is None for v in vals) else sum(vals)

            def total0(field_name: str, res: str, parser) -> float:
                return sum(parser(((c.get("resources") or {}).get(field_name) or {}).get(res)) or 0 for c in containers)

            usage = self.metrics.get((ns, pname))
            created = parse_ts(meta(pod).get("creationTimestamp"))
            node_name = spec.get("nodeName", "")
            ip = status.get("podIP", "")
            terminal = phase in ("Failed", "Succeeded")
            images = {c.get("name"): c.get("image", "") for c in containers + (spec.get("initContainers") or [])}
            cinfo = [_container_info(c, images.get(c.get("name"), ""), init, now)
                     for init, group in ((True, istats), (False, cstats)) for c in group]
            ready_cond = next((c for c in status.get("conditions") or [] if c.get("type") == "Ready"), {})
            not_ready_since = parse_ts(ready_cond.get("lastTransitionTime")) if not ready else None
            deleted = parse_ts(meta(pod).get("deletionTimestamp"))
            owner = controller_ref(pod) or {}
            pinfo = {
                "containerInfo": cinfo, "owner": owner.get("name", ""), "ownerKind": owner.get("kind", ""),
                "notReadyS": int((now - not_ready_since).total_seconds()) if not_ready_since else None,
                "deletingS": int((now - deleted).total_seconds()) if deleted else None,
                "claims": [v["persistentVolumeClaim"]["claimName"] for v in spec.get("volumes") or []
                           if (v.get("persistentVolumeClaim") or {}).get("claimName")],
                "name": pname, "phase": phase, "ready": ready, "restarts": restarts, "recentRestarts": recent,
                "reason": reason, "message": message[:300], "node": node_name, "ip": ip,
                "hostId": f"host:{node_name}" if node_name else UNSCHEDULED,
                "hostNetwork": bool(spec.get("hostNetwork")), "terminal": terminal,
                "deleting": bool(meta(pod).get("deletionTimestamp")), "qos": status.get("qosClass", ""),
                "containers": len(containers),
                "cpu": round(usage[0], 4) if usage else None, "mem": usage[1] if usage else None,
                "cpuLimit": total("limits", "cpu", parse_cpu), "cpuRequest": total("requests", "cpu", parse_cpu),
                "memLimit": total("limits", "memory", parse_mem), "memRequest": total("requests", "memory", parse_mem),
                "ageSeconds": int((now - created).total_seconds()) if created else None,
            }
            wl["pods"].append(pinfo)
            if not wl.get("labels"):
                wl["labels"] = dict(labels_of(pod))
            for c in containers:
                img = _short_image(c.get("image", ""))
                if img and img not in wl["images"]:
                    wl["images"].append(img)
            if node_name and node_name not in wl["hosts"] and not terminal:
                wl["hosts"].append(node_name)
            host = self.nodes.get(f"host:{node_name}")
            if host and not terminal:
                host["podCount"] += 1
                host["cpuUsed"] += (usage[0] if usage else 0)
                host["memUsed"] += (usage[1] if usage else 0)
                host["cpuRequested"] += total0("requests", "cpu", parse_cpu)
                host["memRequested"] += total0("requests", "memory", parse_mem)
                mem_limit = total("limits", "memory", parse_mem)
                if mem_limit is None:
                    host["podsWithoutMemLimit"] += 1
                host["memLimits"] += mem_limit if mem_limit is not None else total0("requests", "memory", parse_mem)
            elif not node_name and phase == "Pending":
                # Pods the scheduler could not place land on a pseudo-host so they stay visible.
                pseudo = self._unscheduled_host()
                pseudo["podCount"] += 1
                pseudo["pending"].append({"pod": pname, "workload": wl["id"], "namespace": ns,
                                          "reason": reason, "message": message[:300],
                                          "memRequest": total0("requests", "memory", parse_mem),
                                          "cpuRequest": total0("requests", "cpu", parse_cpu)})
            self.pods_by_key[key] = pod
            self.pod_wl[key] = wid
            uid = meta(pod).get("uid")
            if uid:
                self.pod_uid[uid] = key
            if not spec.get("hostNetwork") and not terminal:   # evicted pods' IPs get reused
                for pip in self._pod_ips(pod):
                    self.pod_ip[pip] = key

        # Controllers without (running) pods still belong in the picture.
        for (ns, kind, name), obj in self.controllers.items():
            if kind == "ReplicaSet" and (controller_ref(obj) or {}).get("kind") == "Deployment":
                continue
            if kind == "Job" and (controller_ref(obj) or {}).get("kind") == "CronJob":
                continue
            if kind == "ReplicaSet" and int((obj.get("spec") or {}).get("replicas") or 0) == 0:
                continue  # old rollout generations
            labels = labels_of(obj)
            tmpl_labels = (((obj.get("spec") or {}).get("template") or {}).get("metadata") or {}).get("labels") or {}
            if K3S_SVCLB_LABEL in labels or K3S_SVCLB_LABEL in tmpl_labels:
                continue
            wid = f"wl:{ns}/{kind}/{name}"
            if kind == "Job" and wid not in self.nodes:
                st = obj.get("status") or {}
                if not st.get("active"):
                    continue  # finished standalone job
            wl = self._new_workload(ns, kind, name)
            spec = obj.get("spec") or {}
            if kind in ("Deployment", "StatefulSet", "DeploymentConfig", "ReplicaSet"):
                wl["replicas"]["desired"] = int(spec.get("replicas", 1) if spec.get("replicas") is not None else 1)
            elif kind == "DaemonSet":
                wl["replicas"]["desired"] = int((obj.get("status") or {}).get("desiredNumberScheduled") or 0)
            elif kind == "CronJob":
                wl["suspended"] = bool(spec.get("suspend"))
                wl["schedule"] = spec.get("schedule", "")
            template = (spec.get("jobTemplate") or {}).get("spec", {}).get("template") if kind == "CronJob" \
                else spec.get("template")
            if not wl["labels"]:
                wl["labels"] = dict(((template or {}).get("metadata") or {}).get("labels") or {})
            if template and template.get("spec"):
                wl["template"] = template["spec"]
                for c in template["spec"].get("containers") or []:
                    img = _short_image(c.get("image", ""))
                    if img and img not in wl["images"]:
                        wl["images"].append(img)

        for wl in self.nodes.values():
            if wl["kind"] != "workload":
                continue
            pods = [p for p in wl["pods"] if not p["terminal"]]
            wl["replicas"]["current"] = len(pods)
            wl["replicas"]["ready"] = sum(1 for p in pods if p["ready"])
            if wl["subkind"] in ("Pod", "Job", "CronJob", "ReplicationController"):
                wl["replicas"]["desired"] = max(wl["replicas"]["desired"], len(pods))
            if wl["subkind"] == "Pod":
                wl["replicas"]["desired"] = 1

        for hpa in self.snap.get("hpas", []):
            target = (hpa.get("spec") or {}).get("scaleTargetRef") or {}
            wid = f"wl:{ns_of(hpa)}/{target.get('kind')}/{target.get('name')}"
            if wid in self.nodes:
                st, sp = hpa.get("status") or {}, hpa.get("spec") or {}
                self.nodes[wid]["hpa"] = {"min": sp.get("minReplicas", 1), "max": sp.get("maxReplicas"),
                                          "current": st.get("currentReplicas"), "desired": st.get("desiredReplicas")}

    @staticmethod
    def _pod_ips(pod: dict) -> list[str]:
        status = pod.get("status") or {}
        ips = [p.get("ip") for p in status.get("podIPs") or [] if p.get("ip")]
        if status.get("podIP") and status["podIP"] not in ips:
            ips.append(status["podIP"])
        return ips

    # ---------------------------------------------------------------- services
    def _services(self) -> None:
        self.svc: dict[str, dict] = {}
        self.svc_backends: dict[str, dict[str, dict]] = defaultdict(dict)   # svc -> wid -> counts
        self.svc_other_addrs: dict[str, list[str]] = defaultdict(list)
        self.cluster_ip: dict[str, str] = {}
        self.lb_index: dict[tuple[str, int, str], str] = {}
        self.nodeport_index: dict[tuple[int, str], str] = {}
        for svc in self.snap.get("services", []):
            ns, name = ns_of(svc), name_of(svc)
            sid = f"svc:{ns}/{name}"
            spec = svc.get("spec") or {}
            stype = spec.get("type", "ClusterIP")
            lb_ips = [i.get("ip") or i.get("hostname") for i in
                      ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []]
            lb_ips = [i for i in lb_ips if i] + list(spec.get("externalIPs") or [])
            ports = []
            for p in spec.get("ports") or []:
                proto = (p.get("protocol") or "TCP").lower()
                ports.append({"name": p.get("name", ""), "port": p.get("port"), "protocol": proto.upper(),
                              "targetPort": p.get("targetPort"), "nodePort": p.get("nodePort")})
                for ip in lb_ips:
                    if p.get("port"):
                        self.lb_index[(ip, int(p["port"]), proto)] = sid
                if p.get("nodePort") and stype in ("NodePort", "LoadBalancer"):
                    self.nodeport_index[(int(p["nodePort"]), proto)] = sid
            for ip in [spec.get("clusterIP")] + list(spec.get("clusterIPs") or []):
                if ip and ip != "None":
                    self.cluster_ip[ip] = sid
            self.svc[sid] = {"id": sid, "name": name, "namespace": ns, "type": stype, "ports": ports,
                             "clusterIP": spec.get("clusterIP", ""), "externalName": spec.get("externalName", ""),
                             "lbIPs": lb_ips, "selector": spec.get("selector") or {},
                             "exposed": stype in ("LoadBalancer", "NodePort") or bool(spec.get("externalIPs"))}

        have_slices = set()
        for es in self.snap.get("endpointslices", []):
            svc_name = labels_of(es).get("kubernetes.io/service-name")
            if not svc_name:
                continue
            sid = f"svc:{ns_of(es)}/{svc_name}"
            have_slices.add(sid)
            for ep in es.get("endpoints") or []:
                ready = (ep.get("conditions") or {}).get("ready") is not False
                ref = ep.get("targetRef") or {}
                if ref.get("kind") == "Pod":
                    key = f"{ref.get('namespace') or ns_of(es)}/{ref.get('name')}"
                    wid = self.pod_wl.get(key)
                    if wid:
                        counts = self.svc_backends[sid].setdefault(wid, {"ready": 0, "notReady": 0})
                        counts["ready" if ready else "notReady"] += 1
                        continue
                for addr in ep.get("addresses") or []:
                    if addr not in self.svc_other_addrs[sid]:
                        self.svc_other_addrs[sid].append(addr)

        # Fallback for clusters where EndpointSlices are not readable: match selectors.
        for sid, svc in self.svc.items():
            if sid in have_slices or not svc["selector"]:
                continue
            for key, pod in self.pods_by_key.items():
                if ns_of(pod) != svc["namespace"]:
                    continue
                labels = labels_of(pod)
                if all(labels.get(k) == v for k, v in svc["selector"].items()):
                    wid = self.pod_wl[key]
                    counts = self.svc_backends[sid].setdefault(wid, {"ready": 0, "notReady": 0})
                    ready = next((p["ready"] for p in self.nodes[wid]["pods"] if p["name"] == name_of(pod)), False)
                    counts["ready" if ready else "notReady"] += 1

        for sid, backends in self.svc_backends.items():
            svc = self.svc[sid] if sid in self.svc else None
            if not svc:
                continue
            for wid, counts in backends.items():
                summary = {"name": svc["name"], "type": svc["type"], "clusterIP": svc["clusterIP"],
                           "ports": [f"{p['port']}/{p['protocol']}" for p in svc["ports"]],
                           "lbIPs": svc["lbIPs"], "readyEndpoints": counts["ready"],
                           "notReadyEndpoints": counts["notReady"]}
                self.nodes[wid]["services"].append(summary)

    # ---------------------------------------------------------------- entries
    def _entries(self) -> None:
        self.entry_targets: dict[str, list[tuple[str, str]]] = defaultdict(list)   # entry id -> [(svc id, detail)]
        self.entry_class: dict[str, str] = {}
        for ing in self.snap.get("ingresses", []):
            ns, name = ns_of(ing), name_of(ing)
            spec = ing.get("spec") or {}
            eid = f"entry:{ns}/Ingress/{name}"
            hosts = []
            default = ((spec.get("defaultBackend") or {}).get("service") or {}).get("name")
            if default:
                self.entry_targets[eid].append((f"svc:{ns}/{default}", "default backend"))
            for rule in spec.get("rules") or []:
                host = rule.get("host", "*")
                hosts.append(host)
                for path in ((rule.get("http") or {}).get("paths") or []):
                    svc = ((path.get("backend") or {}).get("service") or {})
                    if svc.get("name"):
                        self.entry_targets[eid].append((f"svc:{ns}/{svc['name']}", f"{host}{path.get('path', '/')}"))
            tls = bool(spec.get("tls"))
            self.entry_class[eid] = spec.get("ingressClassName") or \
                (meta(ing).get("annotations") or {}).get("kubernetes.io/ingress.class", "")
            self._entry_node(eid, ns, name, "Ingress", hosts, tls)
        for route in self.snap.get("routes", []):
            ns, name = ns_of(route), name_of(route)
            spec = route.get("spec") or {}
            eid = f"entry:{ns}/Route/{name}"
            for backend in [spec.get("to") or {}] + list(spec.get("alternateBackends") or []):
                if backend.get("kind", "Service") == "Service" and backend.get("name"):
                    self.entry_targets[eid].append((f"svc:{ns}/{backend['name']}", f"weight {backend.get('weight', 100)}"))
            self.entry_class[eid] = "openshift-router"
            self._entry_node(eid, ns, name, "Route", [spec.get("host", "")], bool(spec.get("tls")))
        for hr in self.snap.get("httproutes", []):
            ns, name = ns_of(hr), name_of(hr)
            spec = hr.get("spec") or {}
            eid = f"entry:{ns}/HTTPRoute/{name}"
            for rule in spec.get("rules") or []:
                for ref in rule.get("backendRefs") or []:
                    if ref.get("kind", "Service") == "Service" and ref.get("name"):
                        self.entry_targets[eid].append((f"svc:{ref.get('namespace', ns)}/{ref['name']}", "backendRef"))
            self._entry_node(eid, ns, name, "HTTPRoute", list(spec.get("hostnames") or []), False)

    def _entry_node(self, eid: str, ns: str, name: str, kind: str, hosts: list[str], tls: bool) -> None:
        self.nodes[eid] = {"id": eid, "kind": "entry", "subkind": kind, "name": name, "namespace": ns,
                           "platform": bool(PLATFORM_NS.match(ns)), "hosts": [h for h in hosts if h], "tls": tls}

    def _clients(self, scope: str = "internet", seen_from: str = "") -> str:
        """The outside world calling in. One node for everyone (a crowd, not addresses), split by where
        they come from when the agent can see it: private source addresses are users on your own
        network; public or unseen (behind a load balancer / SNAT) ones are end users on the internet."""
        if scope == "lan":
            nid = "clients:lan"
            if nid not in self.nodes:
                self.nodes[nid] = {"id": nid, "kind": "clients", "subkind": "Users", "name": "Users on your network",
                                   "namespace": "", "scope": "lan", "seenFrom": [],
                                   "scopeWhy": "connections seen arriving from private addresses"}
        else:
            nid = "clients"
            if nid not in self.nodes:
                self.nodes[nid] = {"id": nid, "kind": "clients", "subkind": "Users", "name": "End users",
                                   "namespace": "", "scope": "internet", "seenFrom": [],
                                   "scopeWhy": "anyone reaching your exposed services from outside your network, "
                                               "or whose address is hidden by a load balancer"}
        seen = self.nodes[nid]["seenFrom"]
        if seen_from and seen_from not in seen and len(seen) < 12:
            seen.append(seen_from)
        return nid

    def _is_internal_ip(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(normalize_ip(ip))
        except ValueError:
            return False
        return any(addr in net for net in self.internal_nets if net.version == addr.version)

    def _external(self, key: str, label: str | None = None, detail: str = "") -> str:
        eid = f"ext:{key}"
        if eid not in self.nodes:
            self.nodes[eid] = {"id": eid, "kind": "external", "subkind": "External", "name": label or key,
                               "namespace": "", "addresses": []}
        if detail and detail not in self.nodes[eid]["addresses"]:
            self.nodes[eid]["addresses"].append(detail)
        return eid

    # ---------------------------------------------------------------- declared
    def _declared_edges(self) -> None:
        for eid, targets in self.entry_targets.items():
            acc = self.edges[(self._clients(), eid)]
            acc.evidence.add("declared")
            acc.etype = "exposes"
            for host in self.nodes[eid]["hosts"][:3]:
                acc.add_detail(f"{'https' if self.nodes[eid]['tls'] else 'http'}://{host}")
            for sid, detail in targets:
                sacc = self.svc_edges[(eid, sid)]
                sacc.evidence.add("declared")
                sacc.etype = "route"
                sacc.add_detail(detail)
        for sid, svc in self.svc.items():
            if svc["exposed"]:
                acc = self.svc_edges[(self._clients(), sid)]
                acc.evidence.add("declared")
                acc.etype = "exposes"
                for ip in svc["lbIPs"][:1]:
                    for p in svc["ports"]:
                        acc.add_detail(f"{svc['type']} {ip}:{p['port']}/{p['protocol']}")
                if not svc["lbIPs"]:
                    for p in svc["ports"]:
                        if p.get("nodePort"):
                            acc.add_detail(f"NodePort {p['nodePort']}/{p['protocol']}")
            if svc["type"] == "ExternalName" and svc["externalName"]:
                ext = self._external(svc["externalName"], detail=f"ExternalName {svc['name']}")
                self.external_hosts.add(svc["externalName"])
                svc["externalNode"] = ext

    # ---------------------------------------------------------------- config inference
    def _config_edges(self) -> None:
        templates = {wid: (n["namespace"], n["template"]) for wid, n in self.nodes.items()
                     if n["kind"] == "workload" and n.get("template")}
        refs = inference.infer(templates, self.snap.get("services", []), self.snap.get("configmaps", []))
        for wid, items in refs.items():
            for ref in items:
                if ref.kind == "svc":
                    acc = self.svc_edges[(wid, ref.key)]
                elif ref.kind == "ip":
                    entity = self._resolve(ref.key, ref.port, "tcp")
                    # Node IPs in config are usually "public host" settings, not dependencies.
                    if not entity or entity[0] in ("node", "clients"):
                        continue
                    if entity[0] == "svc":
                        acc = self.svc_edges[(wid, entity[1])]
                    else:
                        target = self._entity_node(entity, as_source=False)
                        if not target or target == wid:
                            continue
                        acc = self.edges[(wid, target)]
                else:
                    self.external_hosts.add(ref.key)
                    acc = self.edges[(wid, self._external(ref.key))]
                acc.evidence.add("config")
                acc.add_detail(ref.detail)
                if ref.port:
                    acc.ports.add(f"{ref.port}/TCP")

    # ---------------------------------------------------------------- observed flows
    def _resolve(self, ip: str, port: int | None, proto: str):
        ip = normalize_ip(ip or "")
        if not ip or ip_is_local(ip):
            return None
        if ip in self.pod_ip:
            key = self.pod_ip[ip]
            if key in self.svclb_pod:
                lb = self.svclb_pod[key]
                return ("svc", lb) if lb.startswith("svc:") else ("clients", "")
            return ("pod", key)
        if ip in self.cluster_ip:
            return ("svc", self.cluster_ip[ip])
        if port is not None and (ip, port, proto) in self.lb_index:
            return ("svc", self.lb_index[(ip, port, proto)])
        if ip in self.node_ips or ip in self.gateway_ips:
            if port is not None and (port, proto) in self.nodeport_index:
                return ("svc", self.nodeport_index[(port, proto)])
            return ("node", self.node_ips.get(ip) or self.gateway_ips[ip])
        return ("ext", ip)

    def _entity_node(self, entity, *, as_source: bool) -> str | None:
        kind, key = entity
        if kind == "pod":
            return self.pod_wl.get(key)
        if kind == "svc":
            return key if key in self.svc else None
        if kind == "clients":
            return self._clients()
        if kind == "node":
            return None if as_source else self._external(f"node/{key}", f"{key} (host)")
        if kind == "ext":
            if as_source:
                return self._clients("lan" if self._is_internal_ip(key) else "internet", key)
            host = self.ext_dns.get(key)
            return self._external(host, detail=key) if host else self._external(key)
        return None

    def _observed_edges(self) -> None:
        if not self.reports:
            self.observations = Observations()
            return
        obs = aggregate(self.reports, self._resolve, lambda uid: self.pod_uid.get(uid),
                        max_idle_s=self.flow_idle_seconds)
        self.observations = obs
        exposed_ports = self._exposed_target_ports()
        for (src, dst), stats in obs.flows.items():
            if src[0] == "node":
                # Host-originated traffic: kubelet probes (drop) or SNAT'd external clients
                # arriving through a NodePort/LoadBalancer (attribute to clients).
                dst_wid = self.pod_wl.get(dst[1]) if dst[0] == "pod" else None
                if dst[0] == "svc" and self.svc.get(dst[1], {}).get("exposed"):
                    src = ("clients", "")
                elif dst_wid and any(p in exposed_ports.get(dst_wid, set()) for p in stats.ports):
                    src = ("clients", "")
                else:
                    continue
            s_node = self._entity_node(src, as_source=True)
            d_node = self._entity_node(dst, as_source=False)
            if not s_node or not d_node or s_node == d_node:
                continue
            if self.nodes.get(s_node, {}).get("kind") == "clients" and d_node.startswith("ext:"):
                continue
            if s_node.startswith("svc:") and d_node.startswith("wl:"):
                # conntrack: which backend answered a VIP - that edge is the Service itself.
                continue
            if d_node.startswith("svc:"):
                self.svc_edges[(s_node, d_node)].add_flow(stats)
            elif s_node.startswith("svc:"):
                continue
            else:
                self.edges[(s_node, d_node)].add_flow(stats)
        for key, ls in obs.listen.items():
            wid = self.pod_wl.get(key)
            if wid:
                cur = self._listen.setdefault(wid, {"queue": 0, "backlog": 0})
                cur["queue"] = max(cur["queue"], ls.queue)
                cur["backlog"] = max(cur["backlog"], ls.backlog)

    def _exposed_target_ports(self) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for sid, svc in self.svc.items():
            if not svc["exposed"]:
                continue
            for wid in self.svc_backends.get(sid, {}):
                for p in svc["ports"]:
                    tp = p.get("targetPort") or p.get("port")
                    if isinstance(tp, int) or (isinstance(tp, str) and tp.isdigit()):
                        out[wid].add(f"{tp}/{p['protocol']}")
        return out

    # ---------------------------------------------------------------- mesh
    def _mesh_edges(self) -> None:
        if not self.mesh:
            return
        by_name: dict[tuple[str, str], str] = {}
        for wid, n in self.nodes.items():
            if n["kind"] == "workload":
                by_name.setdefault((n["namespace"], n["name"]), wid)
        for (sns, sname, dns, dname), m in self.mesh.items():
            s = by_name.get((sns, sname))
            d = by_name.get((dns, dname))
            if not s or not d or s == d:
                continue
            acc = self.edges[(s, d)]
            acc.evidence.add("mesh")
            acc.mesh = m

    # ---------------------------------------------------------------- service folding
    def _collapse_services(self) -> None:
        for (src, sid), acc in list(self.svc_edges.items()):
            svc = self.svc.get(sid)
            if not svc:
                continue
            acc.via.add(f"{svc['namespace']}/{svc['name']}" if not src.startswith(f"wl:{svc['namespace']}/")
                        else svc["name"])
            if sid in self.vip_probes:
                acc.probe = self.vip_probes[sid]
                acc.evidence.add("probe")
            backends = self.svc_backends.get(sid, {})
            if svc.get("externalNode"):
                targets = [svc["externalNode"]]
            elif backends:
                targets = list(backends)
            elif self.svc_other_addrs.get(sid):
                label = "kube-apiserver" if sid == "svc:default/kubernetes" else f"{svc['name']} endpoints"
                key = "kube-apiserver" if sid == "svc:default/kubernetes" else f"svc/{svc['namespace']}/{svc['name']}"
                ext = self._external(key, label)
                for addr in self.svc_other_addrs[sid]:
                    if addr not in self.nodes[ext]["addresses"]:
                        self.nodes[ext]["addresses"].append(addr)
                targets = [ext]
            else:
                if sid not in self.nodes:
                    self.nodes[sid] = {"id": sid, "kind": "service", "subkind": svc["type"], "name": svc["name"],
                                       "namespace": svc["namespace"], "platform": bool(PLATFORM_NS.match(svc["namespace"])),
                                       "ports": [f"{p['port']}/{p['protocol']}" for p in svc["ports"]],
                                       "selector": svc["selector"]}
                targets = [sid]
            for tgt in targets:
                if tgt == src:
                    continue
                self.edges[(src, tgt)].merge(acc)

    def _fold_ingress_controllers(self) -> None:
        """Route ingress traffic through the controller: clients -> controller -> entry -> backend."""
        entries = [eid for eid, n in self.nodes.items() if n["kind"] == "entry"]
        if not entries:
            return
        controllers = []
        for wid, n in self.nodes.items():
            if n["kind"] != "workload":
                continue
            exposed_web = any(s["type"] in ("LoadBalancer", "NodePort") and
                              any(p.split("/")[0] in ("80", "443", "8080", "8443") for p in s["ports"])
                              for s in n["services"])
            named = INGRESS_CONTROLLER.search(n["name"]) or any(INGRESS_CONTROLLER.search(i) for i in n["images"])
            if (exposed_web and named) or (n["namespace"] == "openshift-ingress" and n["name"].startswith("router")):
                controllers.append(wid)
        if not controllers:
            return
        backends_by_entry: dict[str, set[str]] = defaultdict(set)
        for (src, dst) in list(self.edges):
            if src in entries:
                backends_by_entry[src].add(dst)
        for eid in entries:
            cls = self.entry_class.get(eid, "")
            ctrl = next((c for c in controllers if cls and cls in self.nodes[c]["name"]), controllers[0])
            user_edge = self.edges.pop(("clients", eid), None)      # declared exposure is always 'clients'
            acc = self.edges[(ctrl, eid)]
            acc.evidence.add("declared")
            acc.etype = "route"
            if user_edge:
                for d in user_edge.details:
                    acc.add_detail(d)
            for backend in backends_by_entry[eid]:
                direct = self.edges.pop((ctrl, backend), None)
                if direct:
                    if direct.flow:
                        acc.add_flow(direct.flow)
                        self.edges[(eid, backend)].add_flow(direct.flow)
                    if direct.mesh:
                        self.edges[(eid, backend)].mesh = direct.mesh
                        self.edges[(eid, backend)].evidence.add("mesh")

    def _classify_externals(self) -> None:
        """Where an external dependency lives: 'lan' (your own network, outside the cluster),
        'internet' (public), or 'cluster' (the API server / nodes themselves)."""
        host_ips: dict[str, list[str]] = defaultdict(list)
        for ip, host in self.ext_dns.items():
            host_ips[host].append(ip)

        internal = self._is_internal_ip

        for nid, n in self.nodes.items():
            if n["kind"] != "external":
                continue
            if nid == "ext:kube-apiserver" or nid.startswith("ext:node/"):
                n["scope"], n["scopeWhy"] = "cluster", "part of the cluster itself"
                continue
            ips = [a for a in n.get("addresses", []) + [n["name"]] + host_ips.get(n["name"], []) if is_ip(normalize_ip(a))]
            ips = [normalize_ip(a) for a in ips]
            name = n["name"].lower()
            if ips:
                inside = [ip for ip in ips if internal(ip)]
                if len(inside) == len(ips):
                    n["scope"], n["scopeWhy"] = "lan", f"private address {inside[0]}"
                else:
                    n["scope"], n["scopeWhy"] = "internet", f"public address {next(ip for ip in ips if not internal(ip))}"
            elif INTERNAL_SUFFIX.search(name):
                n["scope"], n["scopeWhy"] = "lan", "internal domain name"
            else:
                n["scope"], n["scopeWhy"] = "internet", "public hostname (not resolved)"

    def _cap_externals(self) -> None:
        ext_ids = [nid for nid, n in self.nodes.items() if n["kind"] == "external"]
        if len(ext_ids) <= MAX_EXTERNAL_NODES:
            return

        def weight(nid: str) -> float:
            score = 0.0
            for (s, d), acc in self.edges.items():
                if d == nid:
                    score += 10 if "config" in acc.evidence else 0
                    score += (acc.flow.connections if acc.flow else 0)
            return score

        keep = set(sorted(ext_ids, key=weight, reverse=True)[:MAX_EXTERNAL_NODES - 1])
        other = self._external("other", "Other external endpoints")
        for nid in ext_ids:
            if nid in keep or nid == other:
                continue
            self.nodes[other]["addresses"].extend(self.nodes[nid]["addresses"][:1] or [self.nodes[nid]["name"]])
            del self.nodes[nid]
            for (s, d) in list(self.edges):
                if d == nid:
                    self.edges[(s, other)].merge(self.edges.pop((s, d)))
        self.nodes[other]["addresses"] = self.nodes[other]["addresses"][:40]

    # ---------------------------------------------------------------- output
    def _finalize_edges(self) -> list[dict]:
        out = []
        for (src, dst), acc in self.edges.items():
            if src not in self.nodes or dst not in self.nodes or src == dst:
                continue
            etype = acc.etype
            if self.nodes[src]["kind"] == "clients":
                etype = "exposes"
            elif self.nodes[src]["kind"] == "entry":
                etype = "route"
            traffic: dict[str, Any] = {}
            latency: dict[str, Any] = {}
            health: dict[str, Any] = {}
            if acc.flow:
                f = acc.flow
                traffic["connections"] = max(f.connections, f.connections_in)
                traffic["bytesPerSec"] = round(f.bytes_per_sec, 1)
                if f.idle_s is not None:
                    traffic["lastActiveS"] = round(f.idle_s, 1)
                    # Open but quiet (e.g. a pooled DB connection): a real dependency, not live traffic.
                    traffic["idle"] = f.idle_s > self.flow_idle_seconds
                if f.minrtts_ms:
                    # Path RTT: the kernel's minimum RTT, free of delayed-ACK and app think time.
                    latency["rttMinMs"] = round(percentile(f.minrtts_ms, 0.5), 3)
                if f.rtts_ms:
                    # Smoothed RTT includes the peer's delayed ACKs on quiet / one-way connections
                    # (watch streams show ~10 ms on-node). Shown, compared across nodes, never scored alone.
                    latency["rttP50Ms"] = round(percentile(f.rtts_ms, 0.5), 3)
                    latency["rttP95Ms"] = round(percentile(f.rtts_ms, 0.95), 3)
                health.update({"retrans": f.retrans, "retransTotal": f.retrans_total, "sendQ": f.send_q,
                               "recvQ": f.recv_q, "pending": f.pending})
            if acc.mesh:
                m = acc.mesh
                traffic["rps"] = round(m.rps, 3)
                health["errorRate"] = round(m.err_rate, 4)
                if m.p50_ms is not None:
                    latency["p50Ms"] = round(m.p50_ms, 2)
                if m.p95_ms is not None:
                    latency["p95Ms"] = round(m.p95_ms, 2)
            if acc.probe and acc.probe.attempts:
                latency["connectP50Ms"] = round(acc.probe.p50, 3) if acc.probe.p50 is not None else None
                health["probeFailures"] = acc.probe.failures
                health["probeAttempts"] = acc.probe.attempts
            out.append({
                "id": f"{src}->{dst}", "source": src, "target": dst, "type": etype,
                "evidence": sorted(acc.evidence), "via": sorted(acc.via), "ports": sorted(acc.ports),
                "details": acc.details, "traffic": traffic, "latency": latency, "health": health,
            })
        self._traffic_levels(out)
        return out

    @staticmethod
    def _traffic_levels(edges: list[dict]) -> None:
        def magnitude(e: dict) -> float:
            t = e["traffic"]
            if t.get("idle"):
                return 0.0
            if t.get("rps"):
                return t["rps"] * 2000          # roughly: one request ~ 2 kB
            if t.get("bytesPerSec"):
                return t["bytesPerSec"]
            return t.get("connections", 0) * 500
        mags = [magnitude(e) for e in edges]
        top = max(mags) if mags else 0
        for e, m in zip(edges, mags):
            if top <= 0 or m <= 0:
                e["traffic"]["level"] = 0.0
            else:
                e["traffic"]["level"] = round(max(0.08, math.log1p(m) / math.log1p(top)), 3)

    def _drop_hidden(self, edges: list[dict]) -> None:
        if not self.hidden:
            return
        gone = {nid for nid, n in self.nodes.items() if n.get("namespace") in self.hidden}
        for nid in gone:
            del self.nodes[nid]
        edges[:] = [e for e in edges if e["source"] not in gone and e["target"] not in gone]

    def _namespace_summary(self, graph: dict) -> list[dict]:
        """Per-project scorecard: what is running, what is healthy, and the worst thing wrong."""
        out: dict[str, dict] = {}

        def row(ns: str) -> dict:
            return out.setdefault(ns, {
                "name": ns, "platform": bool(PLATFORM_NS.match(ns)), "workloads": 0, "pods": 0, "warning": 0,
                "critical": 0, "healthy": 0, "degraded": 0, "down": 0, "podsReady": 0, "restarts1h": 0,
                "rolling": 0, "findings": {"critical": 0, "warning": 0, "info": 0}, "top": None, "quota": None})

        for n in graph["nodes"]:
            ns = n.get("namespace")
            if not ns:
                continue
            s = row(ns)
            lvl = (n.get("risk") or {}).get("level")
            if n["kind"] == "workload":
                live = [p for p in n["pods"] if not p["terminal"]]
                s["workloads"] += 1
                s["pods"] += len(n["pods"])
                s["podsReady"] += sum(1 for p in live if p["ready"])
                s["restarts1h"] += sum(p.get("recentRestarts", 0) for p in live)
                if (n.get("rollout") or {}).get("state") in ("progressing", "stuck", "failed"):
                    s["rolling"] += 1
                r = n["replicas"]
                down = r["desired"] > 0 and r["ready"] == 0 and n["subkind"] not in ("CronJob", "Job")
                s["down" if down else "degraded" if lvl in ("warning", "critical") else "healthy"] += 1
            if lvl in ("warning", "critical"):
                s[lvl] += 1
        for f in graph.get("findings", []):
            if not f["namespace"]:
                continue
            s = row(f["namespace"])
            s["findings"][f["severity"]] += 1
            if s["top"] is None and f["severity"] != "info" and f["root"]:
                s["top"] = {"id": f["id"], "title": f["title"], "subjectName": f["subjectName"], "severity": f["severity"]}
        for ns, ratio in getattr(self, "_quota_ratio", {}).items():
            if ns in out:
                out[ns]["quota"] = round(ratio, 3)
        return sorted(out.values(), key=lambda x: (x["platform"], x["name"]))

    def _endpoint_probes(self) -> None:
        for (sid, pod_key), res in self.ep_probes.items():
            wid = self.pod_wl.get(pod_key)
            if not wid or not res.attempts:
                continue
            cur = self._node_probe.setdefault(wid, {"attempts": 0, "failures": 0, "samples": []})
            cur["attempts"] += res.attempts
            cur["failures"] += res.failures
            cur["samples"] += res.samples_ms


def public(graph: dict) -> dict:
    """Strip builder-only fields (pod templates may hold env values) before serving."""
    for n in graph["nodes"]:
        n.pop("template", None)
        n.pop("labels", None)
        n.pop("diag", None)
    for e in graph["edges"]:
        e.pop("diag", None)
    return graph


def filter_graph(graph: dict, namespaces: list[str] | None, include_platform: bool) -> dict:
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
    used_hosts = {p["hostId"] for nid in keep if nodes[nid]["kind"] == "workload" for p in nodes[nid]["pods"]}
    out_nodes = []
    for nid in keep:
        n = dict(nodes[nid])
        n["ghost"] = nid in ghosts
        out_nodes.append(n)
    out_nodes += [dict(n, ghost=False) for n in graph["nodes"] if n["kind"] == "host" and n["id"] in used_hosts]
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
    out["findings"] = [f for f in graph.get("findings", [])
                       if (f["namespace"] and ns_in_scope(f["namespace"], f["platform"]))
                       or (not f["namespace"] and (f["category"] in ("node", "platform") or f["subject"] in keep_ids))]
    out["changes"] = [c for c in graph.get("changes", [])
                      if (c["namespace"] and ns_in_scope(c["namespace"], bool(PLATFORM_NS.match(c["namespace"]))))
                      or (not c["namespace"])]
    return out
