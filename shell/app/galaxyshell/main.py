"""The shell's HTTP app.

    GET /api/v1/modules   the modules, in order, each with whether it is on and how it is doing
    PUT /api/v1/modules/{id}                 turn a module on or off                  (needs a session)
    GET /api/v1/session, POST /api/v1/login, POST /api/v1/logout      the settings area's sign-in
    GET /api/v1/modules/{id}/settings        a module's settings as a form            (needs a session)
    PUT /api/v1/modules/{id}/settings        save them; the module starts using them  (needs a session)
    POST /api/v1/modules/{id}/settings/test  try them without saving                  (needs a session)
    GET /api/v1/version
    GET /healthz /readyz
    GET /                 the page: the bar, and the selected module underneath
"""
from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
from contextlib import asynccontextmanager

import threading

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from galaxy_core.auth import ACTION_HEADER, SESSION_SECONDS, Auth

from . import __build__, __version__
from .config import load_settings
from .probe import Prober, call
from .switches import Switches

log = logging.getLogger("galaxyshell")


def create_app(settings=None, prober: Prober | None = None, caller=call, switches: Switches | None = None) -> FastAPI:
    settings = settings or load_settings()
    switches = switches or Switches(settings.modules_off, settings.data_dir)
    auth = Auth(settings.admin_password)
    by_id = {m["id"]: m for m in settings.module_list}
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    prober = prober or Prober(settings.module_list, settings.probe_seconds, settings.probe_verify_tls)
    prober.enabled = switches.enabled

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("workload-galaxy shell %s (build %s) fronting %s", __version__, __build__,
                 ", ".join(m["id"] + ("" if switches.enabled(m["id"]) else " (off)") for m in settings.module_list) or "no modules")
        prober.start()
        yield
        prober.stop()

    app = FastAPI(title="Workload Galaxy", version=__version__, lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json", redoc_url=None)

    def web_file(rel: str) -> str | None:
        root = os.path.realpath(settings.web_root)
        path = os.path.realpath(os.path.join(root, rel))
        return path if path.startswith(root + os.sep) and os.path.isfile(path) else None

    def build_id() -> str:
        h = hashlib.sha1()
        for dirpath, _dirs, files in sorted(os.walk(settings.web_root)):
            for f in sorted(files):
                st = os.stat(os.path.join(dirpath, f))
                h.update(f"{f}:{st.st_mtime_ns}:{st.st_size};".encode())
        return f"{__version__}-{__build__}-{h.hexdigest()[:8]}"

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        return {"ready": True}

    @app.get("/api/v1/version")
    def version():
        return {"version": __version__, "build": __build__}

    @app.get("/api/v1/modules")
    def modules():
        return {"version": __version__, "build": __build__, "checkEverySeconds": settings.probe_seconds,
                "persistent": switches.persistent, "modules": prober.snapshot()}

    # ------------------------------------------------------------------ the settings area
    # The browser signs in here, once. The shell then speaks to each module on its behalf, presenting
    # the settings token the modules share with it; a module's own credentials pass through and are
    # never kept by the shell.
    def client(request: Request) -> str:
        return request.client.host if request.client else ""

    def action(request: Request) -> None:
        """A header another site's page cannot send, so a stray link cannot change anything."""
        if request.headers.get(ACTION_HEADER, "").lower() != "workload-galaxy":
            raise HTTPException(403, detail="missing the action header")

    def session(request: Request) -> None:
        if not auth.enabled:
            raise HTTPException(403, detail="the settings area is turned off (no admin password is set)")
        if not auth.valid(request.cookies.get(auth.cookie)):
            raise HTTPException(401, detail="sign in to open settings")

    def module(mid: str) -> dict:
        if mid not in by_id:
            raise HTTPException(404, detail="no such module")
        return by_id[mid]

    def relay(status: int, payload: dict, m: dict):
        if status == 0:
            raise HTTPException(502, detail=f"{m['title']} is not reachable ({payload.get('detail') or 'no answer'})")
        if status == 404:
            return {"supported": False}
        if status in (401, 403):
            raise HTTPException(409, detail=f"{m['title']} refused: {payload.get('detail') or 'not allowed'}. "
                                            "The module and the shell must be given the same settings token.")
        if status >= 400:
            raise HTTPException(status if status < 500 else 502, detail=payload.get("detail") or f"{m['title']} answered {status}")
        return {"supported": True, **payload}

    @app.get("/api/v1/session")
    def get_session(request: Request):
        return {"signedIn": auth.valid(request.cookies.get(auth.cookie)), "enabled": auth.enabled,
                "canChange": bool(settings.settings_token), "lockedFor": auth.locked(client(request))}

    @app.post("/api/v1/login")
    async def login(request: Request):
        action(request)
        if not auth.enabled:
            raise HTTPException(403, detail="the settings area is turned off (no admin password is set)")
        wait = auth.locked(client(request))
        if wait:
            raise HTTPException(429, detail=f"too many wrong passwords; try again in {wait} s")
        body = await request.json()
        token = auth.login(client(request), str((body or {}).get("password") or ""))
        if not token:
            raise HTTPException(401, detail="wrong password")
        resp = JSONResponse({"signedIn": True})
        resp.set_cookie(auth.cookie, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/")
        return resp

    @app.post("/api/v1/logout")
    def logout(request: Request):
        action(request)
        resp = JSONResponse({"signedIn": False})
        resp.delete_cookie(auth.cookie, path="/")
        return resp

    @app.put("/api/v1/modules/{mid}")
    async def switch_module(mid: str, request: Request):
        """Turn a module on or off in the suite. The module itself is not touched."""
        action(request)
        session(request)
        m = module(mid)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, detail="send JSON") from None
        on = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(on, bool):
            raise HTTPException(400, detail='send {"enabled": true} or {"enabled": false}')
        try:
            switches.set(mid, on)
        except OSError as err:
            log.warning("could not save that %s is %s: %s", mid, "on" if on else "off", err)
            raise HTTPException(500, detail=f"The choice could not be saved in the shell's data folder ({err.strerror or err}).") from None
        log.info("%s turned %s from %s", mid, "on" if on else "off", client(request))
        if on:
            threading.Thread(target=prober.refresh, daemon=True).start()      # show its state without waiting
        return {"id": mid, "title": m["title"], "enabled": on, "persistent": switches.persistent}

    @app.get("/api/v1/modules/{mid}/settings")
    def module_settings(mid: str, request: Request):
        session(request)
        m = module(mid)
        out = relay(*caller(m, "GET", "api/v1/settings", token=settings.settings_token, verify=settings.probe_verify_tls), m)
        if out.get("supported"):
            out["canSave"] = bool(out.get("writable")) and bool(settings.settings_token)
        return out

    async def forward(mid: str, request: Request, method: str, path: str):
        action(request)
        session(request)
        m = module(mid)
        if not settings.settings_token:
            raise HTTPException(403, detail="the shell has no settings token, so it cannot change a module's settings")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, detail="send JSON") from None
        out = relay(*caller(m, method, path, body=body, token=settings.settings_token, verify=settings.probe_verify_tls), m)
        out["canSave"] = True
        return out

    @app.put("/api/v1/modules/{mid}/settings")
    async def save_module_settings(mid: str, request: Request):
        out = await forward(mid, request, "PUT", "api/v1/settings")
        log.info("settings of %s changed from %s", mid, client(request))
        threading.Thread(target=prober.refresh, daemon=True).start()      # show the module's new state without waiting
        return out

    @app.post("/api/v1/modules/{mid}/settings/test")
    async def test_module_settings(mid: str, request: Request):
        return await forward(mid, request, "POST", "api/v1/settings/test")

    def index():
        path = web_file("index.html")
        if not path:
            return JSONResponse({"detail": "UI not installed"}, status_code=404)
        bid = build_id()
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        html = html.replace('href="css/shell.css"', f'href="v/{bid}/css/shell.css"').replace('src="js/shell.js"', f'src="v/{bid}/js/shell.js"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    app.get("/", include_in_schema=False)(index)
    app.get("/index.html", include_in_schema=False)(index)

    @app.get("/v/{bid}/{rel:path}", include_in_schema=False)
    def asset(bid: str, rel: str):
        path = web_file(rel)
        if not path:
            raise HTTPException(404)
        return FileResponse(path, media_type=mimetypes.guess_type(path)[0],
                            headers={"Cache-Control": "public, max-age=31536000, immutable" if bid == build_id() else "no-cache"})

    return app


app = create_app()
