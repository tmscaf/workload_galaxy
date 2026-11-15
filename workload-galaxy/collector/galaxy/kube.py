"""Read-only access to the Kubernetes / OpenShift API.

Everything is fetched as plain JSON dicts so the topology builder can be tested
with fixtures and never depends on generated model classes. Resources the
cluster does not have (e.g. Routes on vanilla k8s) or that RBAC forbids are
skipped and reported as warnings instead of failing the snapshot.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from kubernetes import client, config

from .config import Settings

log = logging.getLogger(__name__)

# key -> (group-version prefix, plural, namespaced)
RESOURCES: dict[str, tuple[str, str, bool]] = {
    "pods": ("/api/v1", "pods", True),
    "services": ("/api/v1", "services", True),
    "endpointslices": ("/apis/discovery.k8s.io/v1", "endpointslices", True),
    "configmaps": ("/api/v1", "configmaps", True),
    "replicasets": ("/apis/apps/v1", "replicasets", True),
    "replicationcontrollers": ("/api/v1", "replicationcontrollers", True),
    "deployments": ("/apis/apps/v1", "deployments", True),
    "statefulsets": ("/apis/apps/v1", "statefulsets", True),
    "daemonsets": ("/apis/apps/v1", "daemonsets", True),
    "jobs": ("/apis/batch/v1", "jobs", True),
    "cronjobs": ("/apis/batch/v1", "cronjobs", True),
    "ingresses": ("/apis/networking.k8s.io/v1", "ingresses", True),
    "hpas": ("/apis/autoscaling/v2", "horizontalpodautoscalers", True),
    "routes": ("/apis/route.openshift.io/v1", "routes", True),
    "deploymentconfigs": ("/apis/apps.openshift.io/v1", "deploymentconfigs", True),
    "httproutes": ("/apis/gateway.networking.k8s.io/v1", "httproutes", True),
    "podmetrics": ("/apis/metrics.k8s.io/v1beta1", "pods", True),
    "nodes": ("/api/v1", "nodes", False),
    # Troubleshooting sources (diagnose.py): why a deployment is failing, not just that it is.
    "events": ("/api/v1", "events", True),
    "persistentvolumeclaims": ("/api/v1", "persistentvolumeclaims", True),
    "resourcequotas": ("/api/v1", "resourcequotas", True),
    "networkpolicies": ("/apis/networking.k8s.io/v1", "networkpolicies", True),
    "controllerrevisions": ("/apis/apps/v1", "controllerrevisions", True),
    "namespaces": ("/api/v1", "namespaces", False),
    "certificates": ("/apis/cert-manager.io/v1", "certificates", True),
    "clusteroperators": ("/apis/config.openshift.io/v1", "clusteroperators", False),
    "clusterversions": ("/apis/config.openshift.io/v1", "clusterversions", False),
    "machineconfigpools": ("/apis/machineconfiguration.openshift.io/v1", "machineconfigpools", False),
}

# Extra list parameters. Only Warning events are fetched: they are the ones that explain failures,
# and a busy cluster holds tens of thousands of Normal ones.
QUERY: dict[str, dict[str, str]] = {"events": {"fieldSelector": "type=Warning"}}

# Optional resources: a 404 here is normal and not worth a warning.
OPTIONAL = {"routes", "deploymentconfigs", "httproutes", "podmetrics", "replicationcontrollers", "hpas",
            "certificates", "clusteroperators", "clusterversions", "machineconfigpools"}
# Cluster-scoped extras a namespace-scoped (non-admin) install cannot list: skipped quietly.
QUIET_403 = {"namespaces", "clusteroperators", "clusterversions", "machineconfigpools"}

# Fields that are large and never used; dropped to keep snapshots small.
_DROP_META = ("managedFields",)


class KubeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


class KubeSource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.in_cluster = False
        self._lock = threading.Lock()
        self._load_config()
        self.openshift: bool | None = None
        self.cluster_name = "in-cluster" if self.in_cluster else "kubeconfig"

    # -- connection -------------------------------------------------------
    def _load_config(self) -> None:
        try:
            config.load_incluster_config()
            self.in_cluster = True
        except config.ConfigException:
            config.load_kube_config()
            try:
                _, active = config.list_kube_config_contexts()
                self.cluster_name = (active or {}).get("context", {}).get("cluster", "kubeconfig")
            except Exception:  # noqa: BLE001 - cosmetic only
                pass
        self.api = client.ApiClient()

    def get(self, path: str, query: dict[str, Any] | None = None, timeout: float = 30) -> dict:
        params = [(k, v) for k, v in (query or {}).items() if v is not None]
        for attempt in (1, 2):
            req = self.api.param_serialize(
                method="GET", resource_path=path, query_params=params,
                header_params={"Accept": "application/json"}, auth_settings=["BearerToken"],
            )
            resp = self.api.call_api(*req, _request_timeout=timeout)
            resp.read()
            if resp.status == 401 and attempt == 1:
                # exec/OIDC tokens and projected SA tokens rotate; reload once.
                with self._lock:
                    self._load_config()
                continue
            if resp.status >= 400:
                body = (resp.data or b"")[:300].decode("utf-8", "replace")
                raise KubeError(resp.status, body)
            return json.loads(resp.data)
        raise KubeError(401, "unauthorized")

    def get_text(self, path: str, query: dict[str, Any] | None = None, timeout: float = 15) -> str:
        """Raw text GET (pod logs)."""
        params = [(k, v) for k, v in (query or {}).items() if v is not None]
        req = self.api.param_serialize(method="GET", resource_path=path, query_params=params,
                                       header_params={"Accept": "*/*"}, auth_settings=["BearerToken"])
        resp = self.api.call_api(*req, _request_timeout=timeout)
        resp.read()
        if resp.status >= 400:
            raise KubeError(resp.status, (resp.data or b"")[:300].decode("utf-8", "replace"))
        return (resp.data or b"").decode("utf-8", "replace")

    def pod_logs(self, ns: str, pod: str, container: str | None, previous: bool, tail: int = 80) -> str:
        return self.get_text(f"/api/v1/namespaces/{ns}/pods/{pod}/log",
                             {"container": container, "previous": "true" if previous else None,
                              "tailLines": tail, "limitBytes": 65536, "timestamps": "true"})

    def list_path(self, path: str, query: dict[str, str] | None = None, max_items: int = 20000) -> list[dict]:
        items: list[dict] = []
        cont = None
        while True:
            data = self.get(path, {**(query or {}), "limit": 500, "continue": cont})
            for item in data.get("items", []):
                md = item.get("metadata") or {}
                for key in _DROP_META:
                    md.pop(key, None)
                (md.get("annotations") or {}).pop("kubectl.kubernetes.io/last-applied-configuration", None)
                items.append(item)
            cont = (data.get("metadata") or {}).get("continue")
            if not cont or len(items) >= max_items:
                return items

    # -- discovery --------------------------------------------------------
    def detect_openshift(self) -> bool:
        if self.openshift is None:
            try:
                self.get("/apis/route.openshift.io/v1", timeout=10)
                self.openshift = True
            except Exception:  # noqa: BLE001
                self.openshift = False
        return self.openshift

    def list_namespaces(self) -> list[str]:
        if self.settings.namespaces:
            return sorted(self.settings.namespaces)
        try:
            return sorted(i["metadata"]["name"] for i in self.list_path("/api/v1/namespaces"))
        except KubeError as err:
            if err.status != 403:
                raise
        # Non-admin OpenShift users can still list the projects they belong to.
        return sorted(i["metadata"]["name"] for i in self.list_path("/apis/project.openshift.io/v1/projects"))

    # -- snapshot ---------------------------------------------------------
    def snapshot(self) -> dict:
        started = time.monotonic()
        snap: dict[str, Any] = {key: [] for key in RESOURCES}
        warnings: list[str] = []
        scoped = self.settings.namespaces

        jobs: list[tuple[str, str]] = []
        for key, (prefix, plural, namespaced) in RESOURCES.items():
            if key == "configmaps" and not self.settings.read_configmaps:
                continue
            if scoped and not namespaced and key != "nodes":
                continue   # namespace-scoped install: no cluster-level extras
            if namespaced and scoped:
                jobs.extend((key, f"{prefix}/namespaces/{ns}/{plural}") for ns in scoped)
            else:
                jobs.append((key, f"{prefix}/{plural}"))

        def fetch(job: tuple[str, str]):
            key, path = job
            try:
                return key, self.list_path(path, QUERY.get(key)), None
            except KubeError as err:
                if err.status == 404 and key in OPTIONAL:
                    return key, [], None
                if err.status == 403 and key in QUIET_403:
                    return key, [], None
                if err.status == 404 and key == "podmetrics":
                    return key, [], "metrics.k8s.io unavailable (no CPU/memory usage)"
                return key, [], f"{path}: HTTP {err.status}"
            except Exception as err:  # noqa: BLE001 - network errors etc.
                return key, [], f"{path}: {type(err).__name__}: {err}"

        with ThreadPoolExecutor(max_workers=8) as pool:
            for key, items, warning in pool.map(fetch, jobs):
                snap[key].extend(items)
                if warning:
                    warnings.append(warning)

        snap["openshift"] = self.detect_openshift()
        snap["warnings"] = warnings
        snap["fetchSeconds"] = round(time.monotonic() - started, 2)
        snap["clusterName"] = self.cluster_name
        return snap
