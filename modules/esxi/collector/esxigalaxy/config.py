# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""The module's settings, declared once (see galaxy_core.settings) and its manifest."""
from __future__ import annotations

import os

from galaxy_core.settings import Field, load, load_json, schema
from galaxy_core.util import slug

PREFIX = "ESXIGALAXY"
FIELDS = [
    Field("mode", default="live", choices=("live", "sample"), label="Data source", group="Connection",
          help="live reads a real host; sample shows a made-up host with one of each kind of finding."),
    Field("host", label="Host address", group="Connection", required=True,
          help="Name or IP address of the ESXi host (or vCenter), e.g. 192.168.1.10."),
    Field("user", label="User name", group="Connection", required=True,
          help="An account with the built-in Read-only role on the host. Nothing more is needed."),
    Field("password", label="Password", group="Connection", secret=True, required=True, bound_to=("host", "user", "port"),
          help="Kept by the module and never shown again. It is forgotten if the host or user changes, so it cannot be sent somewhere new by accident."),
    Field("port", kind="int", default=443, label="Port", group="Connection", advanced=True),
    Field("verify_tls", kind="bool", default=False, label="Check the host's certificate", group="Connection", advanced=True,
          help="Off by default because hosts ship a self-signed certificate."),
    Field("name", label="Display name", group="Display", help="Shown in the top bar. Empty uses the host's own name."),
    Field("refresh_seconds", kind="int", default=30, minimum=20, label="Refresh every (seconds)", group="Display",
          help="The host's performance counters are 20-second samples, so faster than 20 gains nothing."),
    Field("groups", kind="text", label="VM groups", group="Display",
          help="One group per line, as Title: pattern, pattern. Patterns match VM names (* and ? are wildcards); the first "
               "group that matches wins. Example: Web servers: web-*, proxy-*. Empty groups VMs by guest OS."),
    Field("site_file", kind="path", label="Site file", group="Display", editable=False,
          help="JSON file that sorts VMs into named groups, for deployments that keep them in a file. VM groups above win."),
    Field("data_dir", kind="path", label="Data folder", group="Storage", editable=False,
          help="Where saved settings, usage peaks and the change list are kept between restarts. Empty keeps them in memory."),
    Field("settings_token", label="Settings token", group="Serving", secret=True, editable=False,
          help="Shared with the suite shell, which presents it to change settings. Empty: settings come from the environment only."),
    Field("datastore_warn_pct", kind="int", default=80, minimum=1, label="Datastore warning at (% used)", group="Thresholds", advanced=True),
    Field("datastore_crit_pct", kind="int", default=90, minimum=1, label="Datastore critical at (% used)", group="Thresholds", advanced=True),
    Field("memory_warn_pct", kind="int", default=90, minimum=1, label="Host memory warning at (% used)", group="Thresholds", advanced=True),
    Field("snapshot_days", kind="int", default=7, minimum=1, label="A snapshot is old after (days)", group="Thresholds", advanced=True),
    Field("ready_warn_pct", kind="int", default=5, minimum=1, label="CPU wait warning at (% per vCPU)", group="Thresholds", advanced=True,
          help="How long a VM's vCPUs wait for a physical core before it is reported."),
    Field("web_root", kind="path", label="UI folder", group="Serving", editable=False),
    Field("log_level", default="INFO", label="Log level", group="Serving", editable=False),
]

MANIFEST = {
    "id": "esxi", "title": "ESXi Galaxy", "summary": "A VMware ESXi host: virtual machines, networks, datastores and capacity.",
    "needs": "An ESXi 6.7 or newer host (or vCenter) reachable over HTTPS, and an account with the Read-only role.",
    "envPrefix": PREFIX, "settings": schema(FIELDS, PREFIX),
}


def parse_groups(text: str) -> list[dict]:
    """"Web servers: web-*, proxy-*" per line -> [{id, title, match}]. Lines without a title or a pattern are skipped."""
    out = []
    for line in (text or "").splitlines():
        title, sep, patterns = line.partition(":")
        match = [p.strip() for p in patterns.split(",") if p.strip()]
        if sep and title.strip() and match:
            out.append({"id": slug(title), "title": title.strip(), "match": match})
    return out


def finish(s):
    """What every settings object needs beyond its fields, however it was loaded."""
    s.mode = (s.mode or "live").lower()
    s.log_level = (s.log_level or "INFO").upper()
    if not s.web_root:
        here = os.path.dirname(os.path.abspath(__file__))
        local = os.path.normpath(os.path.join(here, "..", "..", "web"))
        s.web_root = local if os.path.isdir(local) else "/app/web"
    s.site = load_json(s.site_file)
    groups = parse_groups(s.groups)
    if groups:
        s.site = {**s.site, "groups": groups}
    return s


def load_settings(values: dict | None = None, environ=None):
    return finish(load(FIELDS, PREFIX, values, environ))
