"""The shell's settings, and the list of modules it fronts."""
from __future__ import annotations

import os
import re

from galaxy_core.settings import Field, load, load_json

PREFIX = "GALAXY"
FIELDS = [
    Field("modules", kind="list", label="Modules", group="Modules",
          help="Comma-separated id=URL pairs, in the order they should be listed, e.g. "
               "kubernetes=http://galaxy-k8s:8080,network=http://galaxy-net:8080. The URL is the one a browser uses."),
    Field("modules_file", kind="path", label="Modules file", group="Modules", advanced=True,
          help="JSON instead of the list above, when a module needs a title or a separate address for the shell "
               "to check it on: {\"modules\": [{\"id\", \"title\", \"url\", \"probeUrl\"}]}."),
    Field("probe_seconds", kind="int", default=20, minimum=5, label="Check modules every (seconds)", group="Modules", advanced=True),
    Field("probe_verify_tls", kind="bool", default=False, label="Check module certificates", group="Modules", advanced=True),
    Field("admin_password", label="Admin password", group="Settings area", secret=True,
          help="Needed to open the settings area. Empty: the settings area is turned off."),
    Field("settings_token", label="Settings token", group="Settings area", secret=True,
          help="Shared with the modules, which accept settings changes only from a caller that presents it. "
               "Empty: settings can be read here but not changed."),
    Field("web_root", kind="path", label="UI folder", group="Serving", advanced=True),
    Field("log_level", default="INFO", label="Log level", group="Serving", advanced=True),
]
# What a module is called in the selector when it does not say itself.
KNOWN = {"kubernetes": ("Kubernetes", "Workloads, pods, hosts and who calls whom"),
         "network": ("Network", "Devices, what they talk to, Wi-Fi and VPN"),
         "esxi": ("ESXi", "Virtual machines, datastores, networks and capacity")}
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


def parse_modules(pairs: list[str], extra: list[dict] | None = None) -> list[dict]:
    """-> [{id, title, summary, url, probeUrl}], in the order given. Bad entries are dropped, not fatal."""
    out, seen = [], set()
    items = [{"id": p.split("=", 1)[0], "url": p.split("=", 1)[1]} for p in pairs if "=" in p]
    for m in list(extra or []) + items:
        mid, url = str(m.get("id") or "").strip().lower(), str(m.get("url") or "").strip()
        if not ID.match(mid) or not re.match(r"^https?://", url) or mid in seen:
            continue
        seen.add(mid)
        title, summary = KNOWN.get(mid, (mid.replace("-", " ").title(), ""))
        out.append({"id": mid, "title": str(m.get("title") or title), "summary": str(m.get("summary") or summary),
                    "url": url.rstrip("/") + "/", "probeUrl": (str(m.get("probeUrl") or url)).rstrip("/") + "/"})
    return out


def load_settings(values: dict | None = None, environ=None):
    s = load(FIELDS, PREFIX, values, environ)
    s.log_level = (s.log_level or "INFO").upper()
    if not s.web_root:
        here = os.path.dirname(os.path.abspath(__file__))
        local = os.path.normpath(os.path.join(here, "..", "..", "web"))
        s.web_root = local if os.path.isdir(local) else "/app/web"
    s.module_list = parse_modules(s.modules, load_json(s.modules_file).get("modules"))
    return s
