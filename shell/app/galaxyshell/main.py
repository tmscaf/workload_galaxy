"""The shell's HTTP app.

    GET /api/v1/modules   the modules, in order, each with how it is doing
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

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import __build__, __version__
from .config import load_settings
from .probe import Prober

log = logging.getLogger("galaxyshell")


def create_app(settings=None, prober: Prober | None = None) -> FastAPI:
    settings = settings or load_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    prober = prober or Prober(settings.module_list, settings.probe_seconds, settings.probe_verify_tls)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        log.info("workload-galaxy shell %s (build %s) fronting %s", __version__, __build__,
                 ", ".join(m["id"] for m in settings.module_list) or "no modules")
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
                "modules": prober.snapshot()}

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
