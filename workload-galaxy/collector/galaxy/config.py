"""Runtime settings, read from environment variables (see README for the full table)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Ports never connect-probed: closing a socket before the handshake counts as a
# connection error on several databases (MySQL blocks the host after
# max_connect_errors), so probing them would cause the outage we are trying to find.
DEFAULT_PROBE_SKIP_PORTS = "3306,5432,1433,1521,27017,6379,9042,9092,2181,11211,5672,4222,9200,9300"


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    mode: str = "cluster"                      # "cluster" or "demo"
    namespaces: list[str] = field(default_factory=list)          # empty = every namespace RBAC allows
    exclude_namespaces: list[str] = field(default_factory=list)
    self_namespace: str = ""
    hide_self: bool = True
    refresh_seconds: int = 15
    read_configmaps: bool = True
    agent_token: str = ""
    flow_window_seconds: int = 60
    flow_idle_seconds: int = 60
    prometheus_url: str = ""
    prometheus_token_file: str = ""
    prometheus_verify_tls: bool = True
    prometheus_ca_file: str = ""
    probes: bool = False
    probe_interval_seconds: int = 60
    probe_skip_ports: set[int] = field(default_factory=set)
    resolve_external_hosts: bool = True
    internal_cidrs: list[str] = field(default_factory=list)
    event_window_seconds: int = 3600
    pod_logs: bool = False
    web_root: str = ""
    log_level: str = "INFO"


def load_settings() -> Settings:
    here = os.path.dirname(os.path.abspath(__file__))
    default_web = os.path.normpath(os.path.join(here, "..", "..", "web"))
    return Settings(
        mode=os.environ.get("GALAXY_MODE", "cluster").strip().lower(),
        namespaces=_list("GALAXY_NAMESPACES"),
        exclude_namespaces=_list("GALAXY_EXCLUDE_NAMESPACES"),
        self_namespace=os.environ.get("POD_NAMESPACE", ""),
        hide_self=_bool("GALAXY_HIDE_SELF", True),
        refresh_seconds=max(5, _int("GALAXY_REFRESH_SECONDS", 15)),
        read_configmaps=_bool("GALAXY_READ_CONFIGMAPS", True),
        agent_token=os.environ.get("GALAXY_AGENT_TOKEN", ""),
        flow_window_seconds=max(15, _int("GALAXY_FLOW_WINDOW_SECONDS", 60)),
        flow_idle_seconds=max(10, _int("GALAXY_FLOW_IDLE_SECONDS", 60)),
        prometheus_url=os.environ.get("GALAXY_PROMETHEUS_URL", "").rstrip("/"),
        prometheus_token_file=os.environ.get("GALAXY_PROMETHEUS_TOKEN_FILE", ""),
        prometheus_verify_tls=_bool("GALAXY_PROMETHEUS_VERIFY_TLS", True),
        prometheus_ca_file=os.environ.get("GALAXY_PROMETHEUS_CA_FILE", ""),
        probes=_bool("GALAXY_PROBES", False),
        probe_interval_seconds=max(15, _int("GALAXY_PROBE_INTERVAL_SECONDS", 60)),
        probe_skip_ports={int(p) for p in _list("GALAXY_PROBE_SKIP_PORTS", DEFAULT_PROBE_SKIP_PORTS) if p.isdigit()},
        resolve_external_hosts=_bool("GALAXY_RESOLVE_EXTERNAL_HOSTS", True),
        # Extra address ranges to treat as your own network (e.g. corporate public blocks).
        internal_cidrs=_list("GALAXY_INTERNAL_CIDRS"),
        # Warning events older than this no longer explain the present.
        event_window_seconds=max(300, _int("GALAXY_EVENT_WINDOW_SECONDS", 3600)),
        # Off by default: serving log tails widens what the dashboard can reveal (needs pods/log RBAC).
        pod_logs=_bool("GALAXY_POD_LOGS", False),
        web_root=os.environ.get("GALAXY_WEB_ROOT", default_web),
        log_level=os.environ.get("GALAXY_LOG_LEVEL", "INFO").upper(),
    )
