"""The module's settings, declared once for the suite's Settings page (see galaxy_core.settings).

`config.load_settings()` still reads the environment; what is saved from the Settings page is laid
over the environment before it runs (galaxy_core.settingsapi.boot), so both stay in step. Names
here are the environment variable's name without the GALAXY_ prefix, in lower case.
"""
from __future__ import annotations

from galaxy_core.settings import Field, schema

from .config import DEFAULT_PROBE_SKIP_PORTS

PREFIX = "GALAXY"
D, V, M, X, T = "Data source", "What to show", "Service mesh metrics (Prometheus)", "Diagnosis", "Tuning"
FIELDS = [
    Field("mode", default="cluster", choices=("cluster", "demo"), label="Data source", group=D,
          help="cluster reads the cluster this runs in; demo shows a simulated cluster with fault scenarios."),

    Field("namespaces", kind="list", label="Projects to show", group=V,
          help="Comma-separated namespaces. Empty shows every namespace this module is allowed to read."),
    Field("exclude_namespaces", kind="list", label="Projects to leave out", group=V),
    Field("hide_self", kind="bool", default=True, label="Leave this module's own namespace out", group=V),
    Field("group_by", default="prefix", label="Roll projects up by", group=V,
          help="How projects group into business units on large clusters: prefix (the part of the name before the "
               "separator), label:<key> (a namespace label) or none."),
    Field("group_separator", default="-", label="Prefix separator", group=V, advanced=True),
    Field("internal_cidrs", kind="list", label="Your own address ranges", group=V,
          help="Extra ranges to treat as your network and not the internet, e.g. corporate public blocks."),

    Field("prometheus_url", label="Prometheus address", group=M,
          help="For request rates, latency and errors between services. Empty: connections only, from the node agents."),
    Field("prometheus_verify_tls", kind="bool", default=True, label="Check Prometheus's certificate", group=M, advanced=True),

    Field("read_configmaps", kind="bool", default=True, label="Read ConfigMaps to find dependencies", group=X,
          help="Finds calls that have not carried traffic yet from addresses in configuration."),
    Field("pod_logs", kind="bool", default=False, label="Show the last log lines of failing containers", group=X,
          help="Off by default: it widens what this dashboard can reveal, and needs permission to read pod logs."),
    Field("probes", kind="bool", default=False, label="Check that services answer", group=X,
          help="Opens and closes a connection to each service port. Database ports are never probed."),
    Field("probe_interval_seconds", kind="int", default=60, minimum=15, label="Check services every (seconds)", group=X, advanced=True),
    Field("probe_skip_ports", kind="list", default=DEFAULT_PROBE_SKIP_PORTS.split(","), label="Ports never probed", group=X, advanced=True),
    Field("resolve_external_hosts", kind="bool", default=True, label="Look up names for outside addresses", group=X, advanced=True),
    Field("event_window_seconds", kind="int", default=3600, minimum=300, label="Use warning events from the last (seconds)", group=X, advanced=True),
    Field("sizing_min_history_seconds", kind="int", default=3600, minimum=0, label="Watch a workload this long before sizing advice (seconds)",
          group=X, advanced=True),

    Field("refresh_seconds", kind="int", default=15, minimum=5, label="Refresh every (seconds)", group=T, advanced=True),
    Field("flow_window_seconds", kind="int", default=60, minimum=15, label="Count connections seen in the last (seconds)", group=T, advanced=True),
    Field("flow_idle_seconds", kind="int", default=60, minimum=10, label="A connection is idle after (seconds)", group=T, advanced=True),
    Field("detail_limit", kind="int", default=150, minimum=20, label="Draw workloads one by one up to", group=T, advanced=True),
    Field("project_limit", kind="int", default=60, minimum=10, label="Draw projects one by one up to", group=T, advanced=True),
    Field("node_budget", kind="int", default=320, minimum=100, label="Most things drawn at once", group=T, advanced=True),

    Field("agent_token", label="Agent token", group="Serving", secret=True, editable=False),
    Field("prometheus_token_file", kind="path", label="Prometheus token file", group="Serving", editable=False),
    Field("prometheus_ca_file", kind="path", label="Prometheus CA file", group="Serving", editable=False),
    Field("data_dir", kind="path", label="Data folder", group="Serving", editable=False),
    Field("settings_token", label="Settings token", group="Serving", secret=True, editable=False),
    Field("web_root", kind="path", label="UI folder", group="Serving", editable=False),
    Field("log_level", default="INFO", label="Log level", group="Serving", editable=False),
]

MANIFEST = {
    "id": "kubernetes", "title": "Workload Galaxy: Kubernetes",
    "summary": "Kubernetes and OpenShift projects: workloads, pods, hosts, who calls whom, and where latency starts.",
    "needs": "To run inside the cluster it watches, with read access to it. Optional: the node agent, and Prometheus with mesh metrics.",
    "envPrefix": PREFIX, "settings": schema(FIELDS, PREFIX),
}
