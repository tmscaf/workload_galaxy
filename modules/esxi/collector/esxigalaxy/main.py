"""ESXi Galaxy's HTTP app: the shared API (see galaxy_core.webapp) plus what is this module's own.

    GET /api/v1/estate        host-wide totals: capacity, what is allocated, busiest VMs, datastores

The app is made by `create_app()`. Settings come from the environment (ESXIGALAXY_*) and from what
has been saved through the suite's settings page, which wins. Saving starts a fresh reading with
the new settings; nothing needs redeploying.
"""
from __future__ import annotations

import logging
import os

from galaxy_core.settings import SettingsError, Store, missing
from galaxy_core.webapp import Galaxy

from . import __build__, __version__
from .config import FIELDS, MANIFEST, PREFIX, finish
from .findings import CATEGORIES
from .state import EsxiState
from .vsphere import Vsphere, VsphereError

log = logging.getLogger("esxigalaxy")


class Settings:
    """What the suite's settings page talks to: describe the form, try values, save and apply them."""

    def __init__(self, store: Store):
        self.store = store
        self.current = finish(store.effective())
        self.galaxy: Galaxy | None = None

    def describe(self) -> dict:
        d = self.store.describe()
        if self.current.mode == "sample":
            d["missing"] = []                 # sample data needs no host
        return d

    def test(self, values: dict) -> dict:
        s = self.store.preview(values or {})
        if (s.mode or "live").lower() == "sample":
            return {"ok": True, "message": "Sample data needs no connection."}
        unset = missing(FIELDS, s)
        if unset:
            hint = (" The password is not reused when the host or user changes: enter it again."
                    if "Password" in unset and self.current.password else "")
            return {"ok": False, "message": f"Still needed: {', '.join(unset)}.{hint}"}
        client = Vsphere(s.host, s.user, s.password, port=s.port, verify=s.verify_tls, timeout=10)
        try:
            snap = client.snapshot()
        except VsphereError as err:
            return {"ok": False, "message": str(err)}
        finally:
            client.close()
        on = sum(1 for v in snap["vms"] if v["powerState"] == "poweredOn")
        return {"ok": True, "message": f"Connected to {snap['about'].get('fullName') or s.host}: {len(snap['vms'])} VMs "
                                       f"({on} running), {len(snap['datastores'])} datastore{'s' if len(snap['datastores']) != 1 else ''}."}

    def apply(self, values: dict) -> dict:
        forgot = self.store.update(values or {})
        self.current = finish(self.store.effective())
        if self.galaxy is not None:
            self.galaxy.replace_state(EsxiState(self.current), mode=self.current.mode)
        log.info("settings saved (%s); reading again", ", ".join(sorted(values or {})) or "no changes")
        return {"forgot": forgot}


def create_app(settings=None, environ=None):
    """`settings`: a ready settings object (tests); otherwise the environment plus what has been saved."""
    env = os.environ if environ is None else environ
    data_dir = getattr(settings, "data_dir", None) if settings is not None else env.get(f"{PREFIX}_DATA_DIR", "")
    store = Store(FIELDS, PREFIX, os.path.join(data_dir, "settings.json") if data_dir else "", environ=env)
    api = Settings(store)
    if settings is not None:
        api.current = settings
    logging.basicConfig(level=api.current.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    def meta(graph: dict) -> dict:
        s = api.current
        return {"categories": [{"id": c, "title": t} for c, t in CATEGORIES],
                "groups": [{"id": g["name"], "title": g["title"]} for g in graph.get("namespaces", [])],
                "estate": {"name": (graph.get("estate") or {}).get("name") or s.name or s.host},
                "notConfigured": missing(FIELDS, s) if s.mode != "sample" else []}

    galaxy = Galaxy(app_id="esxi-galaxy", title="ESXi Galaxy", version=__version__, build=__build__,
                    state=EsxiState(api.current), web_root=api.current.web_root, mode=api.current.mode, manifest=MANIFEST,
                    meta=meta, calls_sort="bps", triage_extra=lambda full: {"estate": full.get("estate", {})},
                    settings_api=api, settings_token=api.current.settings_token)
    api.galaxy = galaxy

    @galaxy.app.get("/api/v1/estate")
    def estate():
        graph = galaxy.graph_or_503()
        return {"generatedAt": graph["generatedAt"], **graph.get("estate", {})}

    return galaxy.mount_ui()


app = create_app()
