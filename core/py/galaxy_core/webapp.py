"""The HTTP API and UI serving every module exposes.

    GET /api/v1/meta          what is running and which data sources are live
    GET /api/v1/module        the module's manifest: what it is and the settings it takes
    GET /api/v1/namespaces    the groups
    GET /api/v1/topology      the graph; ?ns=a,b limits it to some groups; ?focus=true shows problems only
    GET /api/v1/node          one node or connection with its neighbours and findings
    GET /api/v1/workloads     every workload as a table row (filter, sort, page)
    GET /api/v1/calls         every connection as a table row
    GET /api/v1/triage        findings: what looks wrong, why, what to do
    GET /api/v1/search        find something by name
    GET /healthz /readyz
    GET /                     the UI (works under any path prefix)

`Galaxy(...)` builds the FastAPI app with these routes. A module adds its own routes to `.app`, then
calls `.mount_ui()` last (a mount at "/" would shadow anything registered after it).

The app is created from settings, not at import time, so a host process can create, mount and
replace modules as they are switched on and reconfigured.
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .graphops import filter_graph
from .scale import collapse
from .state import State

log = logging.getLogger(__name__)
RANK = {"critical": 0, "warning": 1, "info": 2}


def split(csv: str | None) -> list[str] | None:
    return [x for x in (csv or "").split(",") if x] or None


class _UIStatic(StaticFiles):
    """App files revalidate on every load so a new deploy is picked up; vendored libraries may be cached."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if not path.startswith("vendor/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


class Galaxy:
    def __init__(self, *, app_id: str, title: str, version: str, build: str, state: State, web_root: str, mode: str,
                 manifest: dict, meta=None, triage_extra=None, calls_sort: str = "bps", detail_limit: int = 400,
                 project_limit: int = 60, node_budget: int = 600):
        self.app_id, self.title, self.version, self.build = app_id, title, version, build
        self.state, self.web_root, self.mode, self.manifest = state, web_root, mode, manifest
        self._meta, self._triage_extra = meta, triage_extra
        self.limits = {"detail_limit": detail_limit, "project_limit": project_limit, "budget": node_budget}
        self.started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._stamp = {"at": 0.0, "value": ""}

        @asynccontextmanager
        async def lifespan(_app: FastAPI):
            log.info("%s %s (build %s) starting (mode=%s)", app_id, version, build, mode)
            state.start()
            yield
            state.stop()

        self.app = FastAPI(title=title, version=version, lifespan=lifespan, docs_url="/api/docs",
                           openapi_url="/api/openapi.json", redoc_url=None)
        self.app.add_middleware(GZipMiddleware, minimum_size=2048)
        self._routes(calls_sort)

    # ------------------------------------------------------------------ helpers a module's own routes use
    def graph_or_503(self) -> dict:
        graph = self.state.current()
        if graph is None:
            raise HTTPException(503, detail=self.state.error or "first reading still loading")
        return graph

    def view(self, ns: str | None, expand: str | None = None, level: str = "auto", pattern: str | None = None,
             focus: bool = False):
        """-> (graph for one screen, {real node id: id it is drawn as}); cached per refresh."""
        self.graph_or_503()
        names = tuple(split(ns) or ())
        exp = tuple(x for x in (expand or "").split(",") if x)

        def build():
            graph, cmap = collapse(filter_graph(self.graph_or_503(), list(names) or None, True, pattern), expand=list(exp),
                                   level=level, focus=focus, **self.limits)
            graph.pop("_scoped", None)
            return graph, cmap
        return self.state.view((names, exp, level, pattern, focus), build)

    def _page(self, rows, ns, q, sort, desc, offset, limit, problems):
        return self.state.index().page(rows, namespaces=split(ns), platform=True, q=q or "", sort=sort, desc=desc,
                                       offset=max(0, offset), limit=max(1, min(500, limit)), only_problems=problems)

    # ------------------------------------------------------------------ the shared API
    def _routes(self, calls_sort: str) -> None:
        app, state = self.app, self.state

        @app.get("/healthz", include_in_schema=False)
        def healthz():
            return {"ok": True}

        @app.get("/readyz", include_in_schema=False)
        def readyz():
            if state.current() is None:
                return JSONResponse({"ready": False, "error": state.error}, status_code=503)
            return {"ready": True}

        @app.get("/api/v1/meta")
        def meta():
            graph = state.current() or {}
            return {"version": self.version, "build": self.build, "startedAt": self.started_at, "assetBuild": self.build_id(),
                    "mode": self.mode, "app": self.app_id, "title": self.title, "updatedAt": state.updated_at or None,
                    "refreshSeconds": state.refresh_seconds, "error": state.error, "cluster": graph.get("cluster"),
                    "sources": graph.get("sources"), "podLogs": False, "inCluster": False,
                    **(self._meta(graph) if self._meta else {})}

        @app.get("/api/v1/version")
        def version():
            return {"version": self.version, "build": self.build, "startedAt": self.started_at}

        @app.get("/api/v1/module")
        def module():
            return {**self.manifest, "version": self.version, "build": self.build, "mode": self.mode}

        @app.get("/api/v1/namespaces")
        def namespaces():
            return {"namespaces": self.graph_or_503().get("namespaces", [])}

        @app.get("/api/v1/topology")
        def topology(ns: str | None = Query(None, description="comma-separated groups"), platform: bool = False,
                     expand: str | None = None, level: str = "auto", focus: bool = Query(False, description="problems only")):
            return self.view(ns, expand, level, focus=focus)[0]

        @app.get("/api/v1/node")
        def node(id: str, ns: str | None = None, platform: bool = False, expand: str | None = None, focus: bool = False):
            detail = state.index().detail(id) if self.graph_or_503() else None
            if not detail:
                raise HTTPException(404, detail="unknown node")
            cmap = self.view(ns, expand, focus=focus)[1]
            src, _, dst = id.partition("->")
            at = cmap.get(id) or (f"{cmap.get(src)}->{cmap.get(dst)}" if dst and cmap.get(src) != cmap.get(dst) else cmap.get(src))
            return {**detail, "at": at}

        @app.get("/api/v1/workloads")
        def workloads(ns: str | None = None, platform: bool = False, q: str | None = None, sort: str = "risk",
                      desc: bool = True, offset: int = 0, limit: int = 100, problems: bool = False):
            self.graph_or_503()
            return self._page(state.index().workload_rows(), ns, q, sort, desc, offset, limit, problems)

        @app.get("/api/v1/calls")
        def calls(ns: str | None = None, platform: bool = False, q: str | None = None, sort: str = calls_sort,
                  desc: bool = True, offset: int = 0, limit: int = 100, problems: bool = False):
            self.graph_or_503()
            return self._page(state.index().call_rows(), ns, q, sort, desc, offset, limit, problems)

        @app.get("/api/v1/search")
        def search(q: str):
            self.graph_or_503()
            return {"hits": state.index().search(q)}

        @app.get("/api/v1/hotspots")
        def hotspots(ns: str | None = None, platform: bool = False):
            graph = self.view(ns, level="workloads")[0]
            return {"generatedAt": graph["generatedAt"], "hotspots": graph["hotspots"], "paths": graph["paths"], "cascades": []}

        @app.get("/api/v1/triage")
        def triage(ns: str | None = None, platform: bool = False,
                   severity: str | None = Query(None, description="critical | warning | info: this level and worse"),
                   pattern: str | None = Query(None, description="a pattern key: return that pattern's members")):
            full = self.graph_or_503()
            graph = self.view(ns, pattern=pattern)[0]
            floor = RANK.get(severity or "info", 2)
            scope = set(split(ns) or [])
            summary = graph["summary"]
            return {"generatedAt": graph["generatedAt"], "cluster": graph.get("cluster"),
                    "counts": {k: summary[k] for k in RANK}, "rootCauses": summary["problems"], "summary": summary,
                    "patterns": [p for p in graph["patterns"] if RANK[p["severity"]] <= floor],
                    "findingsMatching": graph["findingsMatching"],
                    "findings": [f for f in graph["findings"] if RANK[f["severity"]] <= floor],
                    "projects": [p for p in full.get("namespaces", []) if not scope or p["name"] in scope],
                    "changes": graph.get("changes", []), "platform": full.get("platform", {}), "cascades": [],
                    **(self._triage_extra(full) if self._triage_extra else {})}

        @app.get("/api/v1/demo/scenarios")
        def demo_scenarios():
            raise HTTPException(404, detail="this module has no scenarios")

    # ------------------------------------------------------------------ UI
    # The page loads its code from /v/<build>/..., so every deploy has new asset URLs and a browser can
    # never run an older build's JavaScript. The page itself is never cached.
    def build_id(self) -> str:
        now = time.monotonic()
        if now - self._stamp["at"] > 2:
            h = hashlib.sha1()
            root = self.web_root
            for dirpath, dirnames, files in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d != "vendor")
                for f in sorted(files):
                    st = os.stat(os.path.join(dirpath, f))
                    h.update(f"{os.path.relpath(os.path.join(dirpath, f), root)}:{st.st_mtime_ns}:{st.st_size};".encode())
            self._stamp.update(at=now, value=h.hexdigest()[:8])
        return f"{self.version}-{self.build}-{self._stamp['value']}"

    def _web_file(self, rel: str) -> str | None:
        root = os.path.realpath(self.web_root)
        path = os.path.realpath(os.path.join(root, rel))
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            return None   # also blocks ../ traversal
        return path

    def _index(self) -> Response:
        bid = self.build_id()
        path = self._web_file("index.html")
        if not path:
            return JSONResponse({"detail": "UI not installed"}, status_code=404)
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        html = (html.replace('href="css/app.css"', f'href="v/{bid}/css/app.css"')
                    .replace('src="js/main.js"', f'src="v/{bid}/js/main.js"')
                    .replace('<meta charset="utf-8">', f'<meta charset="utf-8">\n  <meta name="galaxy-build" content="{bid}">'))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    def mount_ui(self) -> FastAPI:
        """Register the page and its assets. Call after every API route has been added."""
        app = self.app

        @app.get("/", include_in_schema=False)
        def ui_root():
            return self._index()

        @app.get("/index.html", include_in_schema=False)
        def ui_index():
            return self._index()

        @app.get("/v/{bid}/{rel:path}", include_in_schema=False)
        def ui_versioned(bid: str, rel: str):
            path = self._web_file(rel)
            if not path:
                raise HTTPException(404)
            immutable = bid == self.build_id()
            return FileResponse(path, media_type=mimetypes.guess_type(path)[0],
                                headers={"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"})

        if os.path.isdir(self.web_root):
            app.mount("/", _UIStatic(directory=self.web_root, html=True), name="ui")
        else:
            log.warning("web root %s not found; UI disabled", self.web_root)
        return app
