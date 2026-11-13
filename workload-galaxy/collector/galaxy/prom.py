"""Optional request-level latency from Prometheus (Istio / OpenShift Service Mesh metrics).

On OpenShift point GALAXY_PROMETHEUS_URL at the Thanos querier
(https://thanos-querier.openshift-monitoring.svc:9091) and give the service
account `cluster-monitoring-view`; the projected SA token is used as bearer.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import Settings

log = logging.getLogger(__name__)

GROUP = "source_workload_namespace,source_workload,destination_workload_namespace,destination_workload"
QUERIES = {
    "rps": f'sum by ({GROUP}) (rate(istio_requests_total{{reporter="source"}}[5m]))',
    "err": f'sum by ({GROUP}) (rate(istio_requests_total{{reporter="source",response_code=~"5.."}}[5m]))',
    "p50": f'histogram_quantile(0.5, sum by (le,{GROUP}) (rate(istio_request_duration_milliseconds_bucket{{reporter="source"}}[5m])))',
    "p95": f'histogram_quantile(0.95, sum by (le,{GROUP}) (rate(istio_request_duration_milliseconds_bucket{{reporter="source"}}[5m])))',
}


@dataclass
class MeshEdge:
    src_ns: str
    src: str
    dst_ns: str
    dst: str
    rps: float = 0.0
    err_rate: float = 0.0
    p50_ms: float | None = None
    p95_ms: float | None = None


class PrometheusSource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.prometheus_url)

    def _headers(self) -> dict[str, str]:
        path = self.settings.prometheus_token_file
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                return {"Authorization": f"Bearer {fh.read().strip()}"}
        except OSError as err:
            self.last_error = f"token file: {err}"
            return {}

    def fetch(self) -> dict[tuple[str, str, str, str], MeshEdge]:
        if not self.enabled:
            return {}
        out: dict[tuple[str, str, str, str], MeshEdge] = {}
        try:
            verify = self.settings.prometheus_ca_file or self.settings.prometheus_verify_tls
            with httpx.Client(timeout=15, verify=verify, headers=self._headers()) as http:
                for name, query in QUERIES.items():
                    resp = http.get(f"{self.settings.prometheus_url}/api/v1/query", params={"query": query})
                    resp.raise_for_status()
                    for row in resp.json().get("data", {}).get("result", []):
                        m = row.get("metric", {})
                        key = (m.get("source_workload_namespace", ""), m.get("source_workload", ""),
                               m.get("destination_workload_namespace", ""), m.get("destination_workload", ""))
                        if not key[1] or not key[3] or key[3] == "unknown":
                            continue
                        try:
                            value = float(row["value"][1])
                        except (KeyError, ValueError, IndexError):
                            continue
                        if value != value:  # NaN from histogram_quantile with no data
                            continue
                        edge = out.setdefault(key, MeshEdge(*key))
                        if name == "rps":
                            edge.rps = value
                        elif name == "err":
                            edge.err_rate = value
                        elif name == "p50":
                            edge.p50_ms = value
                        elif name == "p95":
                            edge.p95_ms = value
            for edge in out.values():
                edge.err_rate = edge.err_rate / edge.rps if edge.rps > 0 else 0.0
            self.last_error = None
        except Exception as err:  # noqa: BLE001
            self.last_error = f"{type(err).__name__}: {err}"
            log.warning("prometheus query failed: %s", self.last_error)
        return out
