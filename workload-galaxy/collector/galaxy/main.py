"""HTTP API + static UI.

    GET  /api/v1/meta              what data sources are live
    GET  /api/v1/namespaces        namespace / project summaries
    GET  /api/v1/topology          graph; ?ns=a,b  ?platform=true
    GET  /api/v1/hotspots          ranked problems, riskiest paths, failure cascades (same filters)
    GET  /api/v1/triage            findings (what is wrong, why, what to do), project scorecards, recent changes
    GET  /api/v1/logs              last log lines of one container (only with GALAXY_POD_LOGS=true)
    POST /api/v1/agent/report      node agent ingest (Bearer GALAXY_AGENT_TOKEN)
    GET  /healthz /readyz
    GET  /                         the galaxy UI (works under any path prefix)
"""
from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
import mimetypes

from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __build__, __version__
from .config import load_settings
from .state import GalaxyState
from .scale import collapse
from .topology import filter_graph

settings = load_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("galaxy")
state = GalaxyState(settings)
MAX_REPORT_BYTES = 32 * 1024 * 1024
STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("workload-galaxy %s (build %s) starting (mode=%s, in_cluster=%s)", __version__, __build__,
             settings.mode, state.in_cluster)
    state.start()
    yield
    state.stop()


app = FastAPI(title="Workload Galaxy", version=__version__, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2048)      # JSON compresses ~10x; large estates need it


def _view(ns: str | None, platform: bool, expand: str | None = None, level: str = "auto", pattern: str | None = None):
    """-> (graph for one screen, {real node id: id it is drawn as}); cached per refresh."""
    _graph_or_503()
    names = tuple(_ns(ns) or ())
    exp = tuple(x for x in (expand or "").split(",") if x)

    def build():
        full = _graph_or_503()
        graph, cmap = collapse(filter_graph(full, list(names) or None, platform, pattern), expand=list(exp), level=level,
                               detail_limit=settings.detail_limit, project_limit=settings.project_limit,
                               budget=settings.node_budget)
        graph.pop("_scoped", None)
        return graph, cmap

    return state.view((names, platform, exp, level, pattern), build)


def _graph_or_503() -> dict:
    graph = state.current()
    if graph is None:
        raise HTTPException(503, detail=state.error or "first snapshot still loading")
    return graph


def _ns(ns: str | None) -> list[str] | None:
    return [x for x in (ns or "").split(",") if x] or None


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
    return {"version": __version__, "build": __build__, "startedAt": STARTED_AT, "assetBuild": build_id(),
            "mode": settings.mode, "inCluster": state.in_cluster,
            "updatedAt": state.updated_at or None, "refreshSeconds": settings.refresh_seconds,
            "error": state.error, "cluster": graph.get("cluster"), "sources": graph.get("sources"),
            "podLogs": settings.pod_logs or settings.mode == "demo"}


@app.get("/api/v1/version")
def version():
    """What is actually running: collector build and every agent that has reported."""
    agents = state.flows.status(settings.flow_window_seconds)["agents"]
    if settings.mode == "demo":   # simulated agents, plus any real one that reported
        agents = state.source.agent_status().get("agents", []) + agents
    mismatched = [a["node"] for a in agents if a["version"] != __version__ or (a["build"] and a["build"] != __build__)]
    return {"version": __version__, "build": __build__, "startedAt": STARTED_AT,
            "agents": agents, "agentsMismatched": mismatched}


@app.get("/api/v1/namespaces")
def namespaces():
    return {"namespaces": _graph_or_503().get("namespaces", [])}


@app.get("/api/v1/topology")
def topology(ns: str | None = Query(None, description="comma-separated namespaces/projects"),
             platform: bool = Query(False, description="include platform namespaces (kube-*, openshift-*, cattle-*)"),
             expand: str | None = Query(None, description="units shown in detail: grp:<unit>,ns:<project>, oldest first"),
             level: str = Query("auto", description="auto | workloads | projects | units")):
    """The graph for one screen. Large estates come back rolled up by project or business unit
    (`view` says how); `expand` opens units in place. Small ones are returned in full."""
    return _view(ns, platform, expand, level)[0]


@app.get("/api/v1/node")
def node(id: str, ns: str | None = None, platform: bool = False, expand: str | None = None):
    """One workload, host or call from the full graph, with its neighbours, calls and findings, plus
    `at`: the id it is drawn as in the view the caller is looking at."""
    detail = state.index().detail(id) if _graph_or_503() else None
    if not detail:
        raise HTTPException(404, detail="unknown node")
    cmap = _view(ns, platform, expand)[1]
    src, _, dst = id.partition("->")
    at = cmap.get(id) or (f"{cmap.get(src)}->{cmap.get(dst)}" if dst and cmap.get(src) != cmap.get(dst) else cmap.get(src))
    return {**detail, "at": at}


def _page(rows, ns, platform, q, sort, desc, offset, limit, problems):
    return state.index().page(rows, namespaces=_ns(ns), platform=platform, q=q or "", sort=sort, desc=desc,
                              offset=max(0, offset), limit=max(1, min(500, limit)), only_problems=problems)


@app.get("/api/v1/workloads")
def workloads(ns: str | None = None, platform: bool = False, q: str | None = None, sort: str = "risk",
              desc: bool = True, offset: int = 0, limit: int = 100, problems: bool = False):
    """Every workload as a table row, filtered, sorted and paged on the server."""
    _graph_or_503()
    return _page(state.index().workload_rows(), ns, platform, q, sort, desc, offset, limit, problems)


@app.get("/api/v1/calls")
def calls(ns: str | None = None, platform: bool = False, q: str | None = None, sort: str = "risk",
          desc: bool = True, offset: int = 0, limit: int = 100, problems: bool = False):
    """Every call (upstream -> downstream) as a table row, filtered, sorted and paged on the server."""
    _graph_or_503()
    return _page(state.index().call_rows(), ns, platform, q, sort, desc, offset, limit, problems)


@app.get("/api/v1/search")
def search(q: str):
    _graph_or_503()
    return {"hits": state.index().search(q)}


@app.get("/api/v1/hotspots")
def hotspots(ns: str | None = None, platform: bool = False):
    graph = _view(ns, platform, level="workloads")[0]
    return {"generatedAt": graph["generatedAt"], "hotspots": graph["hotspots"], "paths": graph["paths"],
            "cascades": graph.get("cascades", [])}


@app.get("/api/v1/triage")
def triage(ns: str | None = None, platform: bool = False,
           severity: str | None = Query(None, description="critical | warning | info: this level and worse"),
           pattern: str | None = Query(None, description="a pattern key: return that pattern's members")):
    """The troubleshooting view as data: root causes first, each with cause, next step and commands.
    One problem shared by many workloads comes back once, as a pattern; `pattern=` lists its members."""
    full = _graph_or_503()
    graph = _view(ns, platform, pattern=pattern)[0]
    rank = {"critical": 0, "warning": 1, "info": 2}
    findings = [f for f in graph["findings"] if rank[f["severity"]] <= rank.get(severity or "info", 2)]
    scope = set(_ns(ns) or [])
    projects = [p for p in full.get("namespaces", [])
                if (p["name"] in scope if scope else (platform or not p["platform"]))]
    summary = graph["summary"]
    return {"generatedAt": graph["generatedAt"], "cluster": graph.get("cluster"),
            "counts": {k: summary[k] for k in rank}, "rootCauses": summary["problems"], "summary": summary,
            "patterns": [p for p in graph["patterns"] if rank[p["severity"]] <= rank.get(severity or "info", 2)],
            "findingsMatching": graph["findingsMatching"], "findings": findings, "projects": projects, "changes": graph.get("changes", []),
            "platform": full.get("platform", {}), "cascades": graph.get("cascades", [])}


@app.get("/api/v1/logs")
def logs(ns: str, pod: str, container: str | None = None, previous: bool = False):
    """Tail of one container's log. Off unless GALAXY_POD_LOGS=true: logs can hold what config never shows."""
    if settings.mode == "demo":
        return {"lines": state.source.pod_logs(ns, pod, container, previous), "previous": previous}
    if not settings.pod_logs:
        raise HTTPException(403, detail="log access is disabled (set GALAXY_POD_LOGS=true and grant pods/log)")
    graph = _graph_or_503()
    known = any(n["kind"] == "workload" and n["namespace"] == ns and any(p["name"] == pod for p in n["pods"])
                for n in graph["nodes"])
    if not known:
        raise HTTPException(404, detail="unknown pod")      # only pods this collector shows; never an arbitrary path
    try:
        text = state.source.pod_logs(ns, pod, container, previous)
    except Exception as err:  # noqa: BLE001
        raise HTTPException(502, detail=str(err)[:300]) from err
    return {"lines": text.splitlines()[-80:], "previous": previous}


def _demo_source():
    if settings.mode != "demo":
        raise HTTPException(404, detail="scenarios are only available in demo mode")
    return state.source


@app.get("/api/v1/demo/scenarios")
def demo_scenarios():
    return _demo_source().scenario_info()


@app.put("/api/v1/demo/scenarios")
async def set_demo_scenarios(request: Request):
    src = _demo_source()
    body = await request.json()
    try:
        src.set_scenarios(list(body.get("active") or []))
    except ValueError as err:
        raise HTTPException(400, detail=str(err)) from err
    state.refresh_once()      # apply now instead of waiting for the next tick
    return src.scenario_info()


@app.post("/api/v1/agent/report", status_code=202)
async def agent_report(request: Request):
    if not settings.agent_token:
        raise HTTPException(503, detail="agent ingest disabled: set GALAXY_AGENT_TOKEN")
    auth = request.headers.get("authorization", "")
    if not hmac.compare_digest(auth.encode(), f"Bearer {settings.agent_token}".encode()):
        raise HTTPException(401, detail="bad agent token")
    body = await request.body()
    if len(body) > MAX_REPORT_BYTES:
        raise HTTPException(413)
    if request.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
        if len(body) > MAX_REPORT_BYTES * 4:
            raise HTTPException(413)
    try:
        report = json.loads(body)
    except ValueError as err:
        raise HTTPException(400, detail=f"invalid json: {err}") from err
    if not isinstance(report, dict) or "node" not in report:
        raise HTTPException(400, detail="report must be an object with 'node'")
    state.flows.ingest(report)
    return {"accepted": True}


class _UIStatic(StaticFiles):
    """App files revalidate on every load so a new deploy is picked up; vendored libraries may be cached."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if not path.startswith("vendor/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


# ---------------------------------------------------------------------------------------------- UI
# The page loads its code from /v/<build>/..., so every deploy has new asset URLs: a browser can
# never run an older build's JavaScript (Chrome's normal reload does not revalidate subresources,
# and pre-0.3.0 builds sent JS without cache headers). The page itself is never cached.
BUILD_BASE = f"{__version__}-{__build__}"
_stamp_cache: dict = {"at": 0.0, "value": ""}


def build_id() -> str:
    """<version>-<build>-<fingerprint of the app files>. The fingerprint changes whenever a file
    does, so a browser can never keep a stale copy under an 'immutable' URL - even when files are
    edited under the same build stamp (local development). In an image the files never change."""
    now = time.monotonic()
    if now - _stamp_cache["at"] > 2:
        h = hashlib.sha1()
        root = settings.web_root
        for dirpath, dirnames, files in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d != "vendor")
            for f in sorted(files):
                st = os.stat(os.path.join(dirpath, f))
                h.update(f"{os.path.relpath(os.path.join(dirpath, f), root)}:{st.st_mtime_ns}:{st.st_size};".encode())
        _stamp_cache.update(at=now, value=h.hexdigest()[:8])
    return f"{BUILD_BASE}-{_stamp_cache['value']}"


def _web_file(rel: str) -> str | None:
    root = os.path.realpath(settings.web_root)
    path = os.path.realpath(os.path.join(root, rel))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        return None   # also blocks ../ traversal
    return path


def _index() -> Response:
    bid = build_id()
    path = _web_file("index.html")
    if not path:
        return JSONResponse({"detail": "UI not installed"}, status_code=404)
    with open(path, encoding="utf-8") as fh:
        html = fh.read()
    html = (html.replace('href="css/app.css"', f'href="v/{bid}/css/app.css"')
                .replace('src="js/main.js"', f'src="v/{bid}/js/main.js"')
                .replace('<meta charset="utf-8">', f'<meta charset="utf-8">\n  <meta name="galaxy-build" content="{bid}">'))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/", include_in_schema=False)
def ui_root():
    return _index()


@app.get("/index.html", include_in_schema=False)
def ui_index():
    return _index()


@app.get("/v/{bid}/{rel:path}", include_in_schema=False)
def ui_versioned(bid: str, rel: str):
    path = _web_file(rel)
    if not path:
        raise HTTPException(404)
    # A URL that names the current build never changes content: cache it for good. Anything else
    # (an old tab asking for its build, or a local dev server) must revalidate.
    immutable = bid == build_id()
    return FileResponse(path, media_type=mimetypes.guess_type(path)[0],
                        headers={"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"})


if os.path.isdir(settings.web_root):
    app.mount("/", _UIStatic(directory=settings.web_root, html=True), name="ui")
else:
    log.warning("web root %s not found; UI disabled", settings.web_root)
