"""Synthetic OpenShift-like cluster for GALAXY_MODE=demo.

Produces raw API objects, agent reports and mesh metrics that run through the
exact same builder as a real cluster, with a few deliberate problems:
  * payments/fraud-check is CPU-throttled (single replica) -> slow payments path
  * data/orders-db has a full accept queue and unread socket data
  * storefront/search has a pod in CrashLoopBackOff
  * storefront/cart calls a Service (legacy-pricing) that has no pods
  * payments/payments-api -> api.stripe.com has high internet RTT
"""
from __future__ import annotations

import hashlib
import math
import random
import time

from .prom import MeshEdge
from .scenarios import PRESETS, SCENARIOS, World

HOSTS = [("ocp-worker-1", "10.0.1.11", "us-east-1a"), ("ocp-worker-2", "10.0.2.12", "us-east-1b"),
         ("ocp-worker-3", "10.0.3.13", "us-east-1c")]

# ns, name, kind, replicas, port, cpu limit (cores), cpu use, mem limit (Mi), mem use
WORKLOADS = [
    ("storefront", "web-frontend", "Deployment", 3, 8080, 1.0, 0.35, 512, 260),
    ("storefront", "api-gateway", "Deployment", 3, 8080, 1.0, 0.45, 512, 300),
    ("storefront", "cart", "Deployment", 2, 8080, 0.5, 0.12, 256, 140),
    ("storefront", "catalog", "Deployment", 3, 8080, 1.0, 0.30, 768, 410),
    ("storefront", "search", "Deployment", 3, 9000, 1.0, 0.40, 1024, 600),
    ("storefront", "recommendations", "Deployment", 2, 8080, 1.0, 0.50, 1024, 520),
    ("payments", "payments-api", "Deployment", 2, 8443, 1.0, 0.40, 512, 280),
    ("payments", "fraud-check", "Deployment", 1, 9090, 0.5, 0.485, 512, 330),
    ("payments", "ledger", "StatefulSet", 3, 7000, 1.0, 0.22, 1024, 640),
    ("identity", "auth", "Deployment", 2, 8080, 0.5, 0.12, 256, 150),
    ("identity", "user-profile", "Deployment", 2, 8080, 0.5, 0.10, 256, 170),
    ("identity", "session-cache", "StatefulSet", 1, 6379, 0.5, 0.05, 512, 230),
    ("data", "orders-db", "StatefulSet", 1, 5432, 2.0, 1.55, 4096, 3150),
    ("data", "events-kafka", "StatefulSet", 3, 9092, 2.0, 0.60, 4096, 2400),
    ("data", "search-index", "StatefulSet", 3, 9200, 2.0, 0.90, 4096, 2900),
    ("data", "order-worker", "Deployment", 2, 8080, 0.5, 0.20, 512, 240),
    ("observability", "otel-collector", "DaemonSet", 3, 4317, 0.5, 0.08, 512, 180),
    ("openshift-ingress", "router-default", "Deployment", 2, 443, 1.0, 0.25, 512, 200),
]

# src, dst, rps, p50 ms, p95 ms, error rate, mesh-instrumented
CALLS = [
    ("storefront/web-frontend", "storefront/api-gateway", 220, 18, 45, 0.001, True),
    ("storefront/api-gateway", "storefront/cart", 60, 6, 15, 0.0, True),
    ("storefront/api-gateway", "storefront/catalog", 140, 9, 28, 0.0, True),
    ("storefront/api-gateway", "storefront/search", 45, 22, 160, 0.03, True),
    ("storefront/api-gateway", "identity/auth", 210, 4, 9, 0.0, True),
    ("storefront/api-gateway", "payments/payments-api", 12, 120, 1150, 0.004, True),
    ("storefront/api-gateway", "storefront/recommendations", 30, 35, 80, 0.0, True),
    ("storefront/cart", "identity/session-cache", 80, 0.6, 1.4, 0.0, False),
    ("storefront/cart", "storefront/catalog", 25, 7, 20, 0.0, True),
    ("storefront/catalog", "data/orders-db", 90, 6, 85, 0.0, False),
    ("storefront/search", "data/search-index", 40, 14, 60, 0.0, False),
    ("storefront/recommendations", "storefront/catalog", 15, 8, 22, 0.0, True),
    ("payments/payments-api", "payments/fraud-check", 12, 410, 980, 0.012, True),
    ("payments/payments-api", "payments/ledger", 12, 9, 25, 0.0, True),
    ("payments/payments-api", "data/events-kafka", 12, 2, 6, 0.0, False),
    ("payments/ledger", "data/orders-db", 30, 7, 95, 0.0, False),
    ("identity/auth", "identity/user-profile", 60, 5, 12, 0.0, True),
    ("identity/auth", "identity/session-cache", 210, 0.5, 1.2, 0.0, False),
    ("identity/user-profile", "data/orders-db", 40, 5, 70, 0.0, False),
    ("data/order-worker", "data/events-kafka", 20, 3, 9, 0.0, False),
    ("data/order-worker", "data/orders-db", 20, 8, 110, 0.0, False),
]
EXTERNAL_CALLS = [("payments/payments-api", "api.stripe.com", "198.51.100.20", 443, 140.0),
                  ("identity/auth", "login.microsoftonline.com", "203.0.113.40", 443, 28.0),
                  # on-prem systems: outside the cluster, inside the company network
                  ("data/order-worker", "sftp.acme.internal", "10.20.4.15", 22, 1.8),
                  ("payments/ledger", "mainframe-gw.acme.internal", "10.20.8.30", 1414, 2.4)]
CONFIG_ONLY = [("storefront/recommendations", "data/events-kafka"),
               ("storefront/cart", "storefront/legacy-pricing")]


def _h(text: str, n: int = 10) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:n]


def _uid(text: str) -> str:
    h = hashlib.sha1(text.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


class DemoSource:
    in_cluster = False

    def __init__(self) -> None:
        self.started = time.time()
        self.rng = random.Random(7)
        self.active: list[str] = []
        self.world: World | None = None
        self._build_static()

    # ----------------------------------------------------------------- static objects
    def _build_static(self) -> None:
        self.objs = {k: [] for k in ("pods", "services", "endpointslices", "configmaps", "replicasets",
                                     "replicationcontrollers", "deployments", "statefulsets", "daemonsets", "jobs",
                                     "cronjobs", "ingresses", "hpas", "routes", "deploymentconfigs", "httproutes",
                                     "nodes", "events", "persistentvolumeclaims", "resourcequotas", "networkpolicies",
                                     "controllerrevisions", "namespaces", "certificates", "clusteroperators",
                                     "clusterversions", "machineconfigpools")}
        self.pod_ips: dict[str, list[tuple[str, str, str]]] = {}   # "ns/name" -> [(pod name, ip, uid)]
        self.svc_ip: dict[str, str] = {}
        self.port: dict[str, int] = {}
        self.usage: dict[str, tuple[float, float]] = {}
        created = "2026-10-01T12:00:00Z"
        for name, ip, zone in HOSTS:
            idx = int(name[-1])
            self.objs["nodes"].append({
                "metadata": {"name": name, "labels": {"topology.kubernetes.io/zone": zone,
                                                      "node-role.kubernetes.io/worker": ""}},
                "spec": {"podCIDR": f"10.128.{idx * 2}.0/23"},
                "status": {"addresses": [{"type": "InternalIP", "address": ip}],
                           "allocatable": {"cpu": "15500m", "memory": "32Gi"},
                           "capacity": {"cpu": "16", "memory": "34Gi"},
                           "conditions": [{"type": "Ready", "status": "True"}],
                           "nodeInfo": {"kubeletVersion": "v1.31.6", "osImage": "Red Hat Enterprise Linux CoreOS 418"}},
            })
        call_targets: dict[str, list[str]] = {}
        for src, dst, *_ in CALLS:
            call_targets.setdefault(src, []).append(dst)
        for src, dst in CONFIG_ONLY:
            call_targets.setdefault(src, []).append(dst)
        ext_env = {src: (host, port) for src, host, _ip, port, _rtt in EXTERNAL_CALLS}

        n_svc = 0
        pod_counter = 0
        for ns, name, kind, replicas, port, cpu_l, cpu_u, mem_l, mem_u in WORKLOADS:
            key = f"{ns}/{name}"
            self.port[key] = port
            labels = {"app": name}
            env = []
            for dst in call_targets.get(key, []):
                dns, dname = dst.split("/")
                dport = next((w[4] for w in WORKLOADS if w[0] == dns and w[1] == dname), 8080)
                var = dname.upper().replace("-", "_")
                host = dname if dns == ns else f"{dname}.{dns}.svc.cluster.local"
                env.append({"name": f"{var}_URL", "value": f"http://{host}:{dport}"})
            if key in ext_env:
                host, eport = ext_env[key]
                env.append({"name": "PROVIDER_ENDPOINT", "value": f"https://{host}:{eport}/v1"})
            env.append({"name": "OTEL_EXPORTER_OTLP_ENDPOINT",
                        "value": "http://otel-collector.observability.svc:4317"} if ns != "observability" else
                       {"name": "LOG_LEVEL", "value": "info"})
            container = {"name": name, "image": f"quay.io/acme/{name}:1.{len(name)}.0",
                         "env": env, "ports": [{"containerPort": port}],
                         "resources": {"limits": {"cpu": str(cpu_l), "memory": f"{mem_l}Mi"},
                                       "requests": {"cpu": f"{int(cpu_l * 250)}m", "memory": f"{mem_l // 2}Mi"}}}
            template = {"metadata": {"labels": labels}, "spec": {"containers": [container]}}
            obj = {"metadata": {"name": name, "namespace": ns, "uid": _uid(key), "labels": labels},
                   "spec": {"replicas": replicas, "selector": {"matchLabels": labels}, "template": template},
                   "status": {}}
            plural = {"Deployment": "deployments", "StatefulSet": "statefulsets", "DaemonSet": "daemonsets"}[kind]
            if kind == "DaemonSet":
                obj["status"]["desiredNumberScheduled"] = len(HOSTS)
                replicas = len(HOSTS)
            self.objs[plural].append(obj)
            owner = {"kind": kind, "name": name, "controller": True}
            if kind == "Deployment":
                rs_name = f"{name}-{_h(key, 9)}"
                revision = 3 + len(name) % 9
                # catalog shipped a new version 38 minutes ago; everything else has been stable for weeks.
                recent = key == "storefront/catalog"
                self.objs["replicasets"].append({
                    "metadata": {"name": rs_name, "namespace": ns, "ownerReferences": [owner],
                                 "creationTimestamp": "AGO:2280" if recent else created,
                                 "annotations": {"deployment.kubernetes.io/revision": str(revision)}},
                    "spec": {"replicas": replicas, "template": template}})
                if recent:
                    old = {"metadata": {"labels": labels}, "spec": {"containers": [
                        {**container, "image": "quay.io/acme/catalog:1.6.4",
                         "env": [e for e in env if e["name"] != "OTEL_EXPORTER_OTLP_ENDPOINT"],
                         "resources": {"limits": {"cpu": "1.0", "memory": "512Mi"},
                                       "requests": container["resources"]["requests"]}}]}}
                    self.objs["replicasets"].append({
                        "metadata": {"name": f"{name}-{_h(key + 'old', 9)}", "namespace": ns, "ownerReferences": [owner],
                                     "creationTimestamp": created,
                                     "annotations": {"deployment.kubernetes.io/revision": str(revision - 1)}},
                        "spec": {"replicas": 0, "template": old}})
                owner = {"kind": "ReplicaSet", "name": rs_name, "controller": True}
            self.pod_ips[key] = []
            for i in range(replicas):
                if kind == "StatefulSet":
                    pname = f"{name}-{i}"
                elif kind == "DaemonSet":
                    pname = f"{name}-{_h(key + str(i), 5)}"
                else:
                    pname = f"{owner['name']}-{_h(key + str(i), 5)}"
                host = HOSTS[i % len(HOSTS)] if kind == "DaemonSet" else HOSTS[(pod_counter + i) % len(HOSTS)]
                hidx = int(host[0][-1])
                ip = f"10.128.{hidx * 2}.{10 + pod_counter + i}"
                uid = _uid(f"pod/{key}/{i}")
                crash = key == "storefront/search" and i == 2
                status = {
                    "phase": "Running", "podIP": ip, "podIPs": [{"ip": ip}],
                    "conditions": [{"type": "Ready", "status": "False" if crash else "True"}],
                    "containerStatuses": [{
                        "name": name, "restartCount": 37 if crash else (1 if key == "payments/fraud-check" else 0),
                        "state": {"waiting": {"reason": "CrashLoopBackOff"}} if crash else {"running": {}},
                        "lastState": {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": "RECENT"}}
                        if crash else {},
                    }],
                }
                pod_spec = {"nodeName": host[0], "containers": [container]}
                if kind == "StatefulSet":
                    claim = f"data-{pname}"
                    pod_spec["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": claim}}]
                    self.objs["persistentvolumeclaims"].append({
                        "metadata": {"name": claim, "namespace": ns, "creationTimestamp": created},
                        "spec": {"storageClassName": "gp3-csi", "accessModes": ["ReadWriteOnce"],
                                 "volumeName": f"pvc-{_h(claim, 8)}", "resources": {"requests": {"storage": "50Gi"}}},
                        "status": {"phase": "Bound", "capacity": {"storage": "50Gi"}}})
                self.objs["pods"].append({
                    "metadata": {"name": pname, "namespace": ns, "uid": uid, "labels": dict(labels),
                                 "ownerReferences": [owner], "creationTimestamp": created},
                    "spec": pod_spec,
                    "status": status,
                })
                if crash:
                    self.objs["events"].append({
                        "type": "Warning", "reason": "BackOff", "count": 212, "lastTimestamp": "AGO:45",
                        "involvedObject": {"kind": "Pod", "namespace": ns, "name": pname},
                        "message": f"Back-off restarting failed container {name} in pod {pname}"})
                self.pod_ips[key].append((pname, ip, uid))
                self.usage[f"{ns}/{pname}"] = (cpu_u, mem_u)
            pod_counter += replicas
            n_svc += 1
            cip = f"172.30.{n_svc // 200}.{(n_svc * 7) % 250 + 2}"
            self.svc_ip[key] = cip
            svc_type = "LoadBalancer" if name == "router-default" else "ClusterIP"
            svc = {"metadata": {"name": name, "namespace": ns},
                   "spec": {"type": svc_type, "clusterIP": cip, "selector": labels,
                            "ports": [{"port": port, "protocol": "TCP", "targetPort": port}]},
                   "status": {}}
            if svc_type == "LoadBalancer":
                svc["spec"]["ports"] = [{"name": "https", "port": 443, "protocol": "TCP", "targetPort": 443},
                                        {"name": "http", "port": 80, "protocol": "TCP", "targetPort": 80}]
                svc["status"] = {"loadBalancer": {"ingress": [{"ip": "203.0.113.10"}]}}
            self.objs["services"].append(svc)
        # A Service whose selector matches nothing.
        self.objs["services"].append({"metadata": {"name": "legacy-pricing", "namespace": "storefront"},
                                      "spec": {"type": "ClusterIP", "clusterIP": "172.30.9.9",
                                               "selector": {"app": "legacy-pricing"},
                                               "ports": [{"port": 8080, "protocol": "TCP"}]}})
        self.svc_ip["storefront/legacy-pricing"] = "172.30.9.9"
        self.objs["routes"].append({"metadata": {"name": "shop", "namespace": "storefront"},
                                    "spec": {"host": "shop.acme.example", "to": {"kind": "Service", "name": "web-frontend"},
                                             "tls": {"termination": "edge"}}})
        self.objs["routes"].append({"metadata": {"name": "public-api", "namespace": "storefront"},
                                    "spec": {"host": "api.acme.example", "to": {"kind": "Service", "name": "api-gateway"},
                                             "tls": {"termination": "reencrypt"}}})
        for ns in sorted({w[0] for w in WORKLOADS}):
            self.objs["namespaces"].append({"metadata": {"name": ns, "labels": {
                "kubernetes.io/metadata.name": ns, "tier": "data" if ns == "data" else "apps"}}})
        for op in ("authentication", "console", "dns", "etcd", "image-registry", "ingress", "kube-apiserver",
                   "kube-controller-manager", "kube-scheduler", "machine-config", "monitoring", "network", "storage"):
            self.objs["clusteroperators"].append({"metadata": {"name": op}, "status": {"conditions": [
                {"type": "Available", "status": "True"}, {"type": "Degraded", "status": "False"},
                {"type": "Progressing", "status": "False"}]}})
        self.objs["clusterversions"].append({"metadata": {"name": "version"}, "status": {
            "desired": {"version": "4.18.6"}, "conditions": [{"type": "Progressing", "status": "False"},
                                                             {"type": "Failing", "status": "False"}]}})
        self.objs["machineconfigpools"].append({"metadata": {"name": "worker"}, "status": {
            "machineCount": 3, "updatedMachineCount": 3, "conditions": [{"type": "Updating", "status": "False"},
                                                                        {"type": "Degraded", "status": "False"}]}})
        self.objs["resourcequotas"].append({"metadata": {"name": "compute", "namespace": "payments"}, "status": {
            "hard": {"limits.memory": "8Gi", "limits.cpu": "8", "pods": "20"},
            "used": {"limits.memory": "4608Mi", "limits.cpu": "5500m", "pods": "6"}}})
        self.objs["hpas"].append({"metadata": {"name": "api-gateway", "namespace": "storefront"},
                                  "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "api-gateway"},
                                           "minReplicas": 2, "maxReplicas": 3},
                                  "status": {"currentReplicas": 3, "desiredReplicas": 3}})

    # ----------------------------------------------------------------- scenarios
    def calls(self) -> list[tuple]:
        return CALLS

    def set_scenarios(self, ids: list[str]) -> list[str]:
        unknown = [i for i in ids if i not in SCENARIOS]
        if unknown:
            raise ValueError(f"unknown scenario(s): {', '.join(unknown)}")
        self.active = list(dict.fromkeys(ids))
        return self.active

    def scenario_info(self) -> dict:
        guides = self.world.guides if self.world else {}
        return {"active": self.active, "presets": PRESETS,
                "available": [{"id": s.id, "title": s.title, "summary": s.summary, "layers": list(s.layers),
                               "guide": guides.get(s.id)} for s in SCENARIOS.values()]}

    # ----------------------------------------------------------------- dynamic parts
    def _wave(self, period: float, phase: float = 0.0) -> float:
        return 0.5 + 0.5 * math.sin((time.time() - self.started) / period * 2 * math.pi + phase)

    def snapshot(self) -> dict:
        from datetime import datetime, timedelta, timezone
        recent = (datetime.now(timezone.utc) - timedelta(minutes=4)).isoformat().replace("+00:00", "Z")
        world = World(self)
        for sid in self.active:
            world.guides[sid] = SCENARIOS[sid].apply(world)
        self.world = world
        for p in world.objs["pods"]:
            for c in p["status"].get("containerStatuses") or []:
                term = (c.get("lastState") or {}).get("terminated")
                if term and term.get("finishedAt") == "RECENT":
                    term["finishedAt"] = recent
        for kind in ("replicasets", "events", "jobs", "persistentvolumeclaims", "controllerrevisions", "nodes",
                     "certificates", "routes", "ingresses"):
            for obj in world.objs[kind]:
                _stamp(obj)
        snap = {k: v for k, v in world.objs.items()}
        slices = []
        for svc in world.objs["services"]:
            ns, name = svc["metadata"]["namespace"], svc["metadata"]["name"]
            port = svc["spec"]["ports"][0]["targetPort"] if svc["spec"]["ports"][0].get("targetPort") else \
                svc["spec"]["ports"][0]["port"]
            eps = [{"addresses": [p["status"]["podIP"]],
                    "conditions": {"ready": _ready(p)},
                    "targetRef": {"kind": "Pod", "namespace": ns, "name": p["metadata"]["name"]}}
                   for p in world.pods_of(f"{ns}/{name}")
                   if p["status"].get("podIP") and p["status"].get("phase") == "Running"]
            slices.append({"metadata": {"name": f"{name}-x1", "namespace": ns,
                                        "labels": {"kubernetes.io/service-name": name}},
                           "ports": [{"port": port, "protocol": "TCP"}], "endpoints": eps})
        snap["endpointslices"] = slices
        metrics = []
        for p in world.objs["pods"]:
            ns, pn = p["metadata"]["namespace"], p["metadata"]["name"]
            if p["status"].get("phase") != "Running" or p["spec"].get("nodeName") in world.dead_nodes:
                continue
            if f"{ns}/{pn}" not in world.usage:
                continue
            cpu, mem = world.usage[f"{ns}/{pn}"]
            jitter = 0.92 + 0.08 * self._wave(40, hash(pn) % 7)
            if pn.startswith("fraud-check"):
                jitter = 0.97 + 0.03 * self._wave(25)
            metrics.append({"metadata": {"name": pn, "namespace": ns},
                            "containers": [{"usage": {"cpu": f"{int(cpu * jitter * 1000)}m",
                                                      "memory": f"{int(mem * (0.98 + 0.02 * jitter))}Mi"}}]})
        snap["podmetrics"] = metrics
        snap["openshift"] = True
        snap["warnings"] = []
        snap["fetchSeconds"] = 0.01
        snap["clusterName"] = "demo-ocp"
        return snap

    def _live(self, key: str) -> list[dict]:
        """Running pods with an IP on a reachable node."""
        return [p for p in self.world.pods_of(key) if p["status"].get("phase") == "Running"
                and p["status"].get("podIP") and p["spec"].get("nodeName") not in self.world.dead_nodes]

    def _node_factor(self, pods: list[dict], table: dict[str, float]) -> float:
        if not pods or not table:
            return 1.0
        return 1.0 + sum(table.get(p["spec"]["nodeName"], 1.0) - 1.0 for p in pods) / len(pods)

    def mesh(self) -> dict:
        w = self.world
        out = {}
        load = 0.7 + 0.6 * self._wave(90)
        for src, dst, rps, p50, p95, err, meshed in CALLS:
            if not meshed:
                continue
            sns, sname = src.split("/")
            dns, dname = dst.split("/")
            slow = 1.0 + (0.5 * self._wave(30) if dname == "fraud-check" else 0.1 * self._wave(60, len(dname)))
            dst_pods = [p for p in w.pods_of(dst) if p["status"].get("phase") == "Running"]
            ready = [p for p in dst_pods if _ready(p) and p["spec"]["nodeName"] not in w.dead_nodes]
            src_pods = [p for p in w.pods_of(src) if p["status"].get("phase") == "Running"]
            src_net = {n: 1 + (m - 1) * 0.15 for n, m in w.net_degraded.items()}
            slow *= self._node_factor(ready or dst_pods, w.slow_nodes) * self._node_factor(src_pods, src_net)
            slow *= w.mesh_slow.get(dst, 1.0)
            e = err + w.mesh_err.get(dst, 0.0)
            if (src, dst) in w.blocked or ("*", dst) in w.blocked:
                e, p50, p95 = 1.0, 3000, 5000                   # every call times out
            if dst_pods and not ready:
                e, p50, p95 = max(e, 0.6), 3000, 5000           # every call times out
            elif dst_pods and len(ready) < len(dst_pods):
                e += 0.5 * (1 - len(ready) / len(dst_pods)) * (1 if any(p["spec"]["nodeName"] in w.dead_nodes
                                                                         for p in dst_pods) else 0)
            out[(sns, sname, dns, dname)] = MeshEdge(sns, sname, dns, dname, rps=rps * load, err_rate=min(1.0, e),
                                                     p50_ms=p50 * slow, p95_ms=p95 * slow)
        return out

    def dns(self) -> dict[str, str]:
        return {ip: host for _src, host, ip, _port, _rtt in EXTERNAL_CALLS}

    def pod_logs(self, ns: str, pod: str, container: str | None, previous: bool) -> list[str]:
        """Plausible last lines for a demo pod, matched to the failure it is simulating."""
        p = next((x for x in (self.world.objs["pods"] if self.world else []) if x["metadata"]["name"] == pod
                  and x["metadata"]["namespace"] == ns), None)
        if not p:
            return []
        app = p["metadata"]["labels"].get("app", "app")
        cs = (p["status"].get("containerStatuses") or [{}])[0]
        term = (cs.get("lastState") or {}).get("terminated") or {}
        t = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 95))
        head = [f"{t}.102Z INFO  {app} starting (version {p['spec']['containers'][0]['image'].rsplit(':', 1)[-1]})",
                f"{t}.240Z INFO  loading configuration from environment"]
        if (cs.get("state") or {}).get("waiting", {}).get("reason") in ("ImagePullBackOff", "CreateContainerConfigError"):
            return []                                              # the container never started: no log
        if term.get("exitCode") == 127:
            return [term.get("message") or "exec: command not found"]
        if term.get("reason") == "OOMKilled":
            return head + [f"{t}.911Z INFO  warming model cache (2.1 GiB)", f"{t}.990Z WARN  heap usage 96% of limit",
                           "Killed"]
        if term.get("exitCode") == 137:
            return head + [f"{t}.800Z INFO  listening on :{self.port.get(f'{ns}/{app}', 8080)}",
                           f"{t}.950Z WARN  request queue depth 412, worker pool exhausted",
                           f"{t}.990Z INFO  received SIGTERM, shutting down"]
        if term.get("exitCode"):
            return head + [f"{t}.611Z ERROR failed to connect to index backend search-index.data.svc:9200: "
                           "connection reset by peer", f"{t}.612Z ERROR startup check failed after 3 attempts",
                           f"{t}.613Z FATAL exiting with code {term['exitCode']}"]
        return head + [f"{t}.800Z INFO  listening on :{self.port.get(f'{ns}/{app}', 8080)}",
                       f"{t}.950Z INFO  ready"]

    def agent_status(self) -> dict:
        from . import __build__, __version__
        dead = sorted(self.world.dead_nodes) if self.world else []
        agents = [{"node": h[0], "version": __version__, "build": __build__, "lastReportS": 4.0 if h[0] not in dead else 300.0}
                  for h in HOSTS]
        return {"nodes": len(HOSTS) - len(dead), "known": [h[0] for h in HOSTS], "stale": dead, "agents": agents}

    def reports(self) -> list[dict]:
        w = self.world
        rng = random.Random(int(time.time() / 10))
        alive = [h[0] for h in HOSTS if h[0] not in w.dead_nodes]
        by_node: dict[str, dict] = {n: {"node": n, "receivedAt": time.time(), "netns": {},
                                        "capabilities": {"sockets": "ss", "conntrack": "demo"}} for n in alive}
        ip_pod = {}
        for p in w.objs["pods"]:
            if p["status"].get("podIP") and p["status"].get("phase") == "Running":
                ip_pod[p["status"]["podIP"]] = p

        def netns_for(pod: dict) -> dict | None:
            node = pod["spec"]["nodeName"]
            if node not in by_node:
                return None
            uid = pod["metadata"]["uid"]
            return by_node[node]["netns"].setdefault(uid, {"podUid": uid, "sockets": [], "listen": []})

        def degrade(sock: dict, *pods: dict) -> dict:
            mult = max([w.net_degraded.get(p["spec"]["nodeName"], 1.0) for p in pods] + [1.0])
            if mult > 1 and sock.get("rtt") is not None:
                sock["rtt"] = round(sock["rtt"] * mult * (0.7 + 0.6 * rng.random()), 3)
                sock["retrans"] = rng.choice([0, 1, 2, 3])
                sock["retransTotal"] = (sock.get("retransTotal") or 0) + rng.randint(5, 40)
            cmult = max([w.slow_nodes.get(p["spec"]["nodeName"], 1.0) for p in pods] + [1.0])
            if cmult > 1 and sock.get("rtt") is not None and mult == 1:
                sock["rtt"] = round(sock["rtt"] * (1 + (cmult - 1) * 0.6), 3)
            return sock

        for key in self.port:
            for p in self._live(key):
                ns = netns_for(p)
                if ns is None or not _ready(p):   # a crash-looping container has no listener
                    continue
                q = int(8 + 10 * self._wave(20)) if key == "data/orders-db" else 0
                ns["listen"].append({"ip": "0.0.0.0", "port": self.port[key], "queue": q, "backlog": 128})
                if key == "openshift-ingress/router-default":
                    ns["listen"].append({"ip": "0.0.0.0", "port": 80, "queue": 0, "backlog": 128})

        legacy = [(s, d, 1, 0, 0, 0, False) for s, d in CONFIG_ONLY if "legacy" in d]
        for src, dst, rps, *_ in CALLS + legacy:
            dst_ip = self.svc_ip.get(dst)
            dport = self.port.get(dst, 8080)
            ready_dst = [p for p in self._live(dst) if _ready(p)]
            for sp in self._live(src):
                ns = netns_for(sp)
                if ns is None:
                    continue
                if not ready_dst or (src, dst) in w.blocked or ("*", dst) in w.blocked:
                    # nothing behind the Service, or the connection is dropped on the way: SYN-SENT
                    for _ in range(2 if dst in self.port else 1):
                        ns["sockets"].append({"lip": sp["status"]["podIP"], "lport": 40000 + rng.randint(0, 9999),
                                              "rip": dst_ip, "rport": dport, "state": "SYN-SENT", "dir": "out"})
                    continue
                conns = max(1, min(6, int(rps / 40) + 1))
                for _ in range(conns):
                    target = rng.choice(ready_dst)
                    base_rtt = 0.25 + rng.random() * 0.4
                    if dst == "data/orders-db":
                        base_rtt += 1.5 + 4 * self._wave(20)
                    ns["sockets"].append(degrade({
                        "lip": sp["status"]["podIP"], "lport": 40000 + rng.randint(0, 9999), "rip": dst_ip,
                        "rport": dport, "state": "ESTAB", "dir": "out", "rtt": round(base_rtt, 3),
                        "minrtt": round(0.03 + rng.random() * 0.05, 3), "rq": 0,
                        "sq": rng.choice([0, 0, 0, 2896]) if dst == "data/orders-db" else 0,
                        "retrans": 0, "retransTotal": 0, "bps": rps * 1800 / conns}, sp, target))
                    if dst == "data/orders-db":
                        tns = netns_for(target)
                        if tns is not None:
                            tns["sockets"].append({
                                "lip": target["status"]["podIP"], "lport": dport, "rip": sp["status"]["podIP"],
                                "rport": 40000, "state": "ESTAB", "dir": "in", "rq": rng.choice([0, 1448, 4096]),
                                "sq": 0})
        for src, host, ext_ip, port, rtt in EXTERNAL_CALLS:
            for sp in self._live(src):
                ns = netns_for(sp)
                if ns is None:
                    continue
                ns["sockets"].append({
                    "lip": sp["status"]["podIP"], "lport": 50000, "rip": ext_ip, "rport": port, "state": "ESTAB",
                    "dir": "out", "rtt": round(rtt * (0.9 + 0.3 * rng.random()), 2), "minrtt": round(rtt * 0.85, 2),
                    "retrans": 1 if host == "api.stripe.com" and rng.random() < 0.5 else 0, "retransTotal": 3,
                    "bps": 4000})
        # Users arriving through the router (conntrack view on each node) and router -> backends.
        routers = self._live("openshift-ingress/router-default")
        for h, node in enumerate(alive):
            if not routers:
                break
            r = routers[h % len(routers)]
            by_node[node]["conntrack"] = [
                {"proto": "tcp", "state": "ESTABLISHED", "src": f"198.18.{h}.{i}", "sport": 50000 + i,
                 "dst": "203.0.113.10", "dport": 443, "rsrc": r["status"]["podIP"], "rsport": 443, "bytes": None,
                 "bps": 20000.0} for i in range(12)]
            if h == 0:   # and a few staff on the office network, which the agent can tell apart
                by_node[node]["conntrack"] += [
                    {"proto": "tcp", "state": "ESTABLISHED", "src": f"10.20.12.{i}", "sport": 51000 + i,
                     "dst": "203.0.113.10", "dport": 443, "rsrc": r["status"]["podIP"], "rsport": 443, "bytes": None,
                     "bps": 6000.0} for i in range(1, 4)]
        for r in routers:
            ns = netns_for(r)
            if ns is None:
                continue
            for target in ("storefront/web-frontend", "storefront/api-gateway"):
                for tp in self._live(target):
                    ns["sockets"].append(degrade({
                        "lip": r["status"]["podIP"], "lport": 41000, "rip": tp["status"]["podIP"],
                        "rport": self.port[target], "state": "ESTAB", "dir": "out", "rtt": 0.3, "minrtt": 0.05,
                        "bps": 30000.0},
                        r, tp))
        for rep in by_node.values():
            rep["netns"] = list(rep["netns"].values())
        return list(by_node.values())


def _stamp(obj) -> None:
    """Replace "AGO:<seconds>" placeholders with real timestamps, so demo data is always recent."""
    from datetime import datetime, timedelta, timezone
    items = obj.items() if isinstance(obj, dict) else enumerate(obj) if isinstance(obj, list) else ()
    for k, v in items:
        if isinstance(v, str) and v.startswith("AGO:"):
            obj[k] = (datetime.now(timezone.utc) - timedelta(seconds=float(v[4:]))).isoformat(
                timespec="seconds").replace("+00:00", "Z")
        elif isinstance(v, (dict, list)):
            _stamp(v)


def _ready(pod: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in pod["status"].get("conditions") or [])
