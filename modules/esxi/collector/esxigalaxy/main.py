"""ESXi Galaxy's HTTP app: the shared API (see galaxy_core.webapp) plus what is this module's own.

    GET /api/v1/estate        host-wide totals: capacity, what is allocated, busiest VMs, datastores

The app is made by `create_app(settings)`, so a host process can build it from saved settings. Run on
its own, `app` below is built from the environment (ESXIGALAXY_*).
"""
from __future__ import annotations

import logging

from galaxy_core.settings import missing
from galaxy_core.webapp import Galaxy

from . import __build__, __version__
from .config import FIELDS, MANIFEST, load_settings
from .findings import CATEGORIES
from .state import EsxiState


def create_app(settings=None):
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    state = EsxiState(settings)
    unset = missing(FIELDS, settings) if settings.mode != "sample" else []

    def meta(graph: dict) -> dict:
        return {"categories": [{"id": c, "title": t} for c, t in CATEGORIES],
                "groups": [{"id": g["name"], "title": g["title"]} for g in graph.get("namespaces", [])],
                "estate": {"name": (graph.get("estate") or {}).get("name") or settings.name or settings.host},
                "notConfigured": unset}

    galaxy = Galaxy(app_id="esxi-galaxy", title="ESXi Galaxy", version=__version__, build=__build__, state=state,
                    web_root=settings.web_root, mode=settings.mode, manifest=MANIFEST, meta=meta, calls_sort="bps",
                    triage_extra=lambda full: {"estate": full.get("estate", {})})

    @galaxy.app.get("/api/v1/estate")
    def estate():
        graph = galaxy.graph_or_503()
        return {"generatedAt": graph["generatedAt"], **graph.get("estate", {})}

    return galaxy.mount_ui()


app = create_app()
