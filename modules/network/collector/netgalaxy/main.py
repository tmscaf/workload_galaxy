# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP API + static UI.

    GET /api/v1/meta          what data sources are live
    GET /api/v1/namespaces    device groups
    GET /api/v1/topology      the graph; ?ns=a,b limits it to some groups; ?focus=true shows problems only
    GET /api/v1/node          one device, service or connection with its neighbours and findings
    GET /api/v1/workloads     every device as a table row (filter, sort, page)
    GET /api/v1/calls         every connection as a table row
    GET /api/v1/triage        findings: what looks wrong, why, what to do
    GET /api/v1/network       network-wide totals: traffic, busiest devices and services, firewall activity
    GET /api/v1/search        find a device or service by name, address, MAC or manufacturer
    GET /api/v1/blocks        what you have blocked, and whether the router has applied it
    POST /api/v1/blocks       block an address or range           (needs a session)
    DELETE /api/v1/blocks/x   unblock it                          (needs a session)
    POST /api/v1/login|logout, GET /api/v1/session
    GET /api/v1/blocklist     the list the router's sync script pulls (token + router address only)
    GET /api/v1/vpn           who is connected to the VPN, recent connects/disconnects/failed logins
    GET /api/v1/wifi          Wi-Fi and connection health: the path to the internet hop by hop, access points, Wi-Fi devices
    GET /api/v1/wifi/device   one Wi-Fi device: where its connection breaks, its path, 24 hours of history   (?mac=)
    POST /api/v1/wifi/watch   ping a device every few seconds instead of every half minute   {mac, on}
    POST /api/v1/wifi/test    ping a device, its access point, the router and the internet side by side, now   {mac}
    POST /api/v1/vpn-status   the router's sync script reports the VPN server's client list (token + router only)
    GET /api/v1/plugins       the module's plugins and which are on
    GET /api/v1/plugins/<id>  what a plugin's page shows; a plugin may add routes of its own under it
    GET|PUT /api/v1/settings, POST /api/v1/settings/test   the suite's Settings page (settings token only)
    GET /healthz /readyz
    GET /                     the UI (works under any path prefix)

The app never connects to the router to change it. Blocking works by the router pulling the list
from /api/v1/blocklist, validating it, and loading it into a firewall table.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import threading
import mimetypes
import os
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from galaxy_core import settingsapi

from . import __build__, __version__
from .auth import ACTION_HEADER, COOKIE, SESSION_SECONDS, Auth
from .blocks import BlockError
from .config import load_settings
from .fields import FIELDS, MANIFEST, PREFIX
from .devices import GROUPS
from .findings import CATEGORIES
from .graphops import filter_graph
from .plugins import PLUGINS
from .scale import collapse
from .state import NetState

# What was saved from the suite's Settings page is laid over the environment before it is read.
STORE, BASE_ENV = settingsapi.boot(FIELDS, PREFIX)
settings = load_settings()
logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("netgalaxy")
# Plugins that are switched on. Each gets what the module knows, never the router's credentials.
plugins = PLUGINS.load(SimpleNamespace(
    mode=settings.mode, data_dir=settings.data_dir, router_configured=bool(settings.blocklist_token),
    counts_blocked=bool(settings.sinkhole_ips), graph=lambda: state.current(),
    router_only=lambda request, token: _router_only(request, token)))
state = NetState(settings, plugins)
auth = Auth(settings.admin_password)
STARTED_AT = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("network-galaxy %s (build %s) starting (mode=%s)", __version__, __build__, settings.mode)
    state.start()
    yield
    state.stop()


app = FastAPI(title="Network Galaxy", version=__version__, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2048)


def _graph_or_503() -> dict:
    graph = state.current()
    if graph is None:
        raise HTTPException(503, detail=state.error or "first reading still loading")
    return graph


def _ns(ns: str | None) -> list[str] | None:
    return [x for x in (ns or "").split(",") if x] or None


def _view(ns: str | None, expand: str | None = None, level: str = "auto", pattern: str | None = None, focus: bool = False):
    """-> (graph for one screen, {real node id: id it is drawn as}); cached per refresh."""
    _graph_or_503()
    names = tuple(_ns(ns) or ())
    exp = tuple(x for x in (expand or "").split(",") if x)

    def build():
        full = _graph_or_503()
        graph, cmap = collapse(filter_graph(full, list(names) or None, True, pattern), expand=list(exp), level=level,
                               detail_limit=settings.detail_limit, project_limit=settings.project_limit,
                               budget=settings.node_budget, focus=focus)
        graph.pop("_scoped", None)
        return graph, cmap
    return state.view((names, exp, level, pattern, focus), build)


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
            "mode": settings.mode, "app": "network-galaxy", "updatedAt": state.updated_at or None,
            "refreshSeconds": settings.refresh_seconds, "error": state.error, "cluster": graph.get("cluster"),
            "sources": graph.get("sources"), "podLogs": False, "inCluster": False,
            "groups": [{"id": g, "title": t} for g, t in GROUPS], "categories": [{"id": c, "title": t} for c, t in CATEGORIES],
            "ntopngUrl": settings.ntopng_url, "network": {"name": settings.network_name},
            "plugins": plugins.describe()}


@app.get("/api/v1/version")
def version():
    return {"version": __version__, "build": __build__, "startedAt": STARTED_AT}


@app.get("/api/v1/namespaces")
def namespaces():
    return {"namespaces": _graph_or_503().get("namespaces", [])}


@app.get("/api/v1/topology")
def topology(ns: str | None = Query(None, description="comma-separated device groups"),
             platform: bool = False, expand: str | None = None, level: str = "auto",
             focus: bool = Query(False, description="problems only")):
    return _view(ns, expand, level, focus=focus)[0]


@app.get("/api/v1/node")
def node(id: str, ns: str | None = None, platform: bool = False, expand: str | None = None, focus: bool = False):
    detail = state.index().detail(id) if _graph_or_503() else None
    if not detail:
        raise HTTPException(404, detail="unknown node")
    cmap = _view(ns, expand, focus=focus)[1]
    src, _, dst = id.partition("->")
    at = cmap.get(id) or (f"{cmap.get(src)}->{cmap.get(dst)}" if dst and cmap.get(src) != cmap.get(dst) else cmap.get(src))
    return {**detail, "at": at}


def _page(rows, ns, q, sort, desc, offset, limit, problems):
    return state.index().page(rows, namespaces=_ns(ns), platform=True, q=q or "", sort=sort, desc=desc,
                              offset=max(0, offset), limit=max(1, min(500, limit)), only_problems=problems)


@app.get("/api/v1/workloads")
def workloads(ns: str | None = None, platform: bool = False, q: str | None = None, sort: str = "risk",
              desc: bool = True, offset: int = 0, limit: int = 100, problems: bool = False):
    """Every device as a table row, filtered, sorted and paged on the server."""
    _graph_or_503()
    return _page(state.index().workload_rows(), ns, q, sort, desc, offset, limit, problems)


@app.get("/api/v1/calls")
def calls(ns: str | None = None, platform: bool = False, q: str | None = None, sort: str = "bps",
          desc: bool = True, offset: int = 0, limit: int = 100, problems: bool = False):
    """Every connection (device -> service or device) as a table row."""
    _graph_or_503()
    return _page(state.index().call_rows(), ns, q, sort, desc, offset, limit, problems)


@app.get("/api/v1/search")
def search(q: str):
    _graph_or_503()
    return {"hits": state.index().search(q)}


@app.get("/api/v1/hotspots")
def hotspots(ns: str | None = None, platform: bool = False):
    graph = _view(ns, level="workloads")[0]
    return {"generatedAt": graph["generatedAt"], "hotspots": graph["hotspots"], "paths": graph["paths"], "cascades": []}


@app.get("/api/v1/network")
def network():
    graph = _graph_or_503()
    return {"generatedAt": graph["generatedAt"], **graph.get("network", {})}


@app.get("/api/v1/triage")
def triage(ns: str | None = None, platform: bool = False,
           severity: str | None = Query(None, description="critical | warning | info: this level and worse"),
           pattern: str | None = Query(None, description="a pattern key: return that pattern's members")):
    full = _graph_or_503()
    graph = _view(ns, pattern=pattern)[0]
    rank = {"critical": 0, "warning": 1, "info": 2}
    floor = rank.get(severity or "info", 2)
    scope = set(_ns(ns) or [])
    summary = graph["summary"]
    return {"generatedAt": graph["generatedAt"], "cluster": graph.get("cluster"),
            "counts": {k: summary[k] for k in rank}, "rootCauses": summary["problems"], "summary": summary,
            "patterns": [p for p in graph["patterns"] if rank[p["severity"]] <= floor],
            "findingsMatching": graph["findingsMatching"],
            "findings": [f for f in graph["findings"] if rank[f["severity"]] <= floor],
            "projects": [p for p in full.get("namespaces", []) if not scope or p["name"] in scope],
            "changes": graph.get("changes", []), "platform": full.get("platform", {}), "cascades": [],
            "network": full.get("network", {})}


# ---------------------------------------------------------------------------------------------- blocking
def _client(request: Request) -> str:
    return request.client.host if request.client else ""


def _require_session(request: Request) -> str:
    if not auth.enabled:
        raise HTTPException(403, detail="actions are turned off (no admin password is set)")
    if request.headers.get(ACTION_HEADER, "").lower() != "network-galaxy":
        raise HTTPException(403, detail="missing the action header")
    if not auth.valid(request.cookies.get(COOKIE)):
        raise HTTPException(401, detail="sign in to block or unblock")
    return "admin"


def _refresh_soon() -> None:
    """Show the change on the map without waiting for the next refresh."""
    threading.Thread(target=lambda: state.refresh_once(), daemon=True).start()


@app.get("/api/v1/session")
def session(request: Request):
    return {"signedIn": auth.valid(request.cookies.get(COOKIE)), "actions": auth.enabled,
            "routerSync": bool(settings.blocklist_token), "lockedFor": auth.locked(_client(request))}


@app.post("/api/v1/login")
async def login(request: Request):
    if not auth.enabled:
        raise HTTPException(403, detail="actions are turned off (no admin password is set)")
    if request.headers.get(ACTION_HEADER, "").lower() != "network-galaxy":
        raise HTTPException(403, detail="missing the action header")
    wait = auth.locked(_client(request))
    if wait:
        raise HTTPException(429, detail=f"too many wrong passwords; try again in {wait} s")
    body = await request.json()
    token = auth.login(_client(request), str(body.get("password") or ""))
    if not token:
        raise HTTPException(401, detail="wrong password")
    resp = JSONResponse({"signedIn": True})
    resp.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/")
    return resp


@app.post("/api/v1/logout")
def logout():
    resp = JSONResponse({"signedIn": False})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.get("/api/v1/blocks")
def blocks():
    return state.blocks.snapshot()


@app.post("/api/v1/blocks")
async def add_block(request: Request):
    who = _require_session(request)
    body = await request.json()
    targets = body.get("targets") or ([body["target"]] if body.get("target") else [])
    if not targets:
        raise HTTPException(400, detail="nothing to block")
    added, errors = [], []
    for t in targets[:16]:
        try:
            added.append(state.blocks.add(str(t), reason=str(body.get("reason") or ""), by=who,
                                          expires=str(body.get("expires") or "7d"), finding=str(body.get("finding") or ""),
                                          label=str(body.get("label") or "")))
        except BlockError as err:
            errors.append({"target": t, "error": str(err)})
    if added:
        log.info("blocked %s (%s)", ", ".join(e["target"] for e in added), body.get("reason") or "no reason given")
        _refresh_soon()
    if errors and not added:
        raise HTTPException(400, detail="; ".join(e["error"] for e in errors))
    return {"added": added, "errors": errors, **state.blocks.snapshot()}


@app.delete("/api/v1/blocks/{target:path}")
def remove_block(target: str, request: Request):
    who = _require_session(request)
    try:
        entry = state.blocks.remove(target, by=who)
    except BlockError as err:
        raise HTTPException(404, detail=str(err)) from err
    log.info("unblocked %s", entry["target"])
    _refresh_soon()
    return {"removed": entry, **state.blocks.snapshot()}


@app.get("/api/v1/blocklist", include_in_schema=False)
def blocklist(request: Request, token: str = "", have: str = "", rejected: int | None = None):
    """The list the router's sync script pulls: one address or range per line."""
    sender = _router_only(request, token)
    state.blocks.router_fetch(have=have or None, rejected=rejected, sender=sender)
    body, digest = state.blocks.text()
    return PlainTextResponse(body, headers={"X-Blocklist-Hash": digest, "Cache-Control": "no-store"})


def _router_only(request: Request, token: str) -> str:
    """The checks every request from the router's sync script passes: its address and its token."""
    if not settings.blocklist_token:
        raise HTTPException(404)
    sender = _client(request)
    if settings.blocklist_senders and sender not in settings.blocklist_senders:
        raise HTTPException(403, detail="not an allowed sender")
    if not hmac.compare_digest(token.encode(), settings.blocklist_token.encode()):
        raise HTTPException(403, detail="bad token")
    return sender


@app.get("/api/v1/vpn")
def vpn():
    return state.vpn.snapshot()


@app.post("/api/v1/vpn-status", include_in_schema=False)
async def vpn_status(request: Request):
    """The VPN server's list of connected clients, posted by the router once a minute."""
    sender = _router_only(request, request.headers.get("X-NetGalaxy-Token", ""))
    raw = await request.body()
    if len(raw) > 256 * 1024:
        raise HTTPException(413, detail="too large")
    try:
        import json as _json
        count = state.vpn.ingest_status(_json.loads(raw or b"{}"), sender=sender)
    except ValueError as err:
        raise HTTPException(400, detail=f"unreadable status: {err}") from err
    return {"ok": True, "sessions": count}


# ---------------------------------------------------------------------------------------------- Wi-Fi and connection health
_MAC = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
_test_lock = threading.Lock()


def _action_header(request: Request) -> None:
    """Changes need a header a web page on another site cannot send, so a stray link cannot trigger them."""
    if request.headers.get(ACTION_HEADER, "").lower() != "network-galaxy":
        raise HTTPException(403, detail="missing the action header")


@app.get("/api/v1/wifi")
def wifi():
    return state.wifi.snapshot()


@app.get("/api/v1/wifi/device")
def wifi_device(mac: str):
    detail = state.wifi.device(mac) if _MAC.match(mac or "") else None
    if not detail:
        raise HTTPException(404, detail="that device is not on the Wi-Fi (and is not being watched)")
    return detail


@app.post("/api/v1/wifi/watch")
async def wifi_watch(request: Request):
    """Watch a device: ping it every few seconds, and keep pinging while it is off the Wi-Fi."""
    _action_header(request)
    body = await request.json()
    mac = str(body.get("mac") or "")
    if not _MAC.match(mac):
        raise HTTPException(400, detail="not a MAC address")
    on = bool(body.get("on", True))
    if on and mac.upper() not in state.wifi.clients:
        raise HTTPException(404, detail="that device is not on the Wi-Fi right now")
    if on and len(state.wifi.watch) >= 30 and mac.upper() not in state.wifi.watch:
        raise HTTPException(400, detail="30 devices are being watched already; stop watching one first")
    watch = state.wifi.set_watch(mac, on)
    state.pinger.set_targets(state.wifi.targets())
    return {"watching": sorted(watch)}


@app.post("/api/v1/wifi/test")
async def wifi_test(request: Request):
    """Ping the device, its access point, the router, the ISP gateway and the internet side by side for a few seconds."""
    _action_header(request)
    body = await request.json()
    detail = state.wifi.device(str(body.get("mac") or "")) if _MAC.match(str(body.get("mac") or "")) else None
    if not detail:
        raise HTTPException(404, detail="that device is not on the Wi-Fi")
    hops = [(h["id"], h["name"], h["ip"]) for h in detail["path"] if h.get("ip") and (h["id"] == "internet" or settings.is_lan(h["ip"])
                                                                                    or h["ip"] == settings.isp_gateway)]
    if not _test_lock.acquire(blocking=False):
        raise HTTPException(429, detail="a test is already running; try again in a few seconds")
    try:
        if settings.mode == "sample":
            now = time.time()
            rows = {hid: [state.sample.ping_ms(hid, now - i) for i in range(12)] for hid, _n, _ip in hops}
            results = {hid: _summarise(ip, rows[hid]) for hid, _n, ip in hops}
        else:
            from .pinger import burst
            try:
                results = await _in_thread(burst, {hid: ip for hid, _n, ip in hops})
            except OSError as err:
                raise HTTPException(503, detail=f"cannot send pings from here: {err.strerror or err}") from err
    finally:
        _test_lock.release()
    return {"at": time.time(), "hops": [{"id": hid, "name": name, **results[hid]} for hid, name, _ip in hops if hid in results]}


async def _in_thread(fn, *args):
    import asyncio
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


def _summarise(ip: str, rows: list) -> dict:
    got = sorted(r for r in rows if r is not None)
    return {"ip": ip, "sent": len(rows), "received": len(got), "lossPct": round(100.0 * (len(rows) - len(got)) / len(rows), 1),
            "minMs": got[0] if got else None, "p50Ms": got[len(got) // 2] if got else None, "maxMs": got[-1] if got else None,
            "jitterMs": None, "samples": rows}


# ---------------------------------------------------------------------------------------------- settings, for the suite
def _test_settings(c) -> dict:
    """Try the router and the Wi-Fi controller with these settings, without saving them."""
    from .ntopng import Ntopng, NtopngError
    from .unifi import Unifi, UnifiError
    need = [label for label, value in (("ntopng address", c.ntopng_url), ("ntopng user", c.ntopng_user),
                                      ("ntopng password", c.ntopng_password)) if not value]
    if need:
        return {"ok": False, "message": f"Still needed: {', '.join(need)}."}
    lines, ok = [], True
    try:
        iface = Ntopng(c.ntopng_url.rstrip("/"), c.ntopng_user, c.ntopng_password, ifid=c.ntopng_ifid, verify=c.ntopng_verify_tls,
                       timeout=10).interface()
        lines.append(f"ntopng answered: {iface.get('num_local_hosts', iface.get('num_hosts', 0))} devices, "
                     f"{iface.get('num_flows', 0)} connections.")
    except NtopngError as err:
        ok = False
        lines.append(f"ntopng: {err}")
    if c.unifi_url:
        if not (c.unifi_user and c.unifi_password):
            ok = False
            lines.append("UniFi: the controller user and password are needed.")
        else:
            try:
                snap = Unifi(c.unifi_url.rstrip("/"), c.unifi_user, c.unifi_password, site=c.unifi_site or "default",
                             verify=c.unifi_verify_tls, timeout=10).snapshot()
                lines.append(f"UniFi answered: {len(snap.get('aps') or snap.get('devices') or [])} access points and switches, "
                             f"{len(snap.get('clients') or [])} Wi-Fi devices.")
            except UnifiError as err:
                ok = False
                lines.append(f"UniFi: {err}")
    return {"ok": ok, "message": " ".join(lines)}


settingsapi.attach(app, api=settingsapi.RestartingSettings(STORE, BASE_ENV, tester=_test_settings, before_restart=lambda: state.stop()),
                   token=os.environ.get(f"{PREFIX}_SETTINGS_TOKEN", ""), manifest=MANIFEST, mode=lambda: settings.mode)


@app.get("/api/v1/module")
def module_manifest():
    return {**MANIFEST, "version": __version__, "build": __build__, "mode": settings.mode, "plugins": plugins.describe()}


plugins.mount(app)


@app.get("/api/v1/demo/scenarios")
def demo_scenarios():
    raise HTTPException(404, detail="there are no scenarios in Network Galaxy")


class _UIStatic(StaticFiles):
    """App files revalidate on every load so a new deploy is picked up; vendored libraries may be cached."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if not path.startswith("vendor/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


# ---------------------------------------------------------------------------------------------- UI
# The page loads its code from /v/<build>/..., so every deploy has new asset URLs and a browser can
# never run an older build's JavaScript. The page itself is never cached.
BUILD_BASE = f"{__version__}-{__build__}"
_stamp_cache: dict = {"at": 0.0, "value": ""}


def build_id() -> str:
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
    immutable = bid == build_id()
    return FileResponse(path, media_type=mimetypes.guess_type(path)[0],
                        headers={"Cache-Control": "public, max-age=31536000, immutable" if immutable else "no-cache"})


if os.path.isdir(settings.web_root):
    app.mount("/", _UIStatic(directory=settings.web_root, html=True), name="ui")
else:
    log.warning("web root %s not found; UI disabled", settings.web_root)
