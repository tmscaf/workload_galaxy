# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Plugins: optional add-ons inside one module.

A plugin adds one job to a module that already watches something: it has its own settings, its
own switch (off until someone turns it on), and may add findings, API routes and a page of its
own. It lives in the module's source tree and is listed by name in the module's code:

    PLUGINS = Plugins([AdBlocker], prefix="NETGALAXY")

Nothing is discovered or loaded at run time. A plugin runs inside a service that holds the
module's credentials, so only plugins that ship with the suite, and were reviewed with it, exist.

The rule modules follow holds for plugins too: a plugin watches. Where one has to change the
system being watched, it publishes something that system fetches and checks for itself; it never
reaches in.

What a plugin class provides:

    id, title, summary      who it is ("adblock", "Ad blocker", one sentence)
    fields                  its settings, as galaxy_core.settings.Field; names start with "<id>_"
    tab                     {"title", "hint"} when it has a page of its own; the page's code is the
                            module's web/js/plugins/<id>.js
    start() / stop()        background work, only called when the plugin is on
    routes(router, ctx)     add its API under /api/v1/plugins/<id>/...
    snapshot()              what its page shows (served at /api/v1/plugins/<id>)
    source()                a short status for the module's "sources" (and for diagnose)
    diagnose(d)             add findings, with the module's own Diagnoser-like object

Each plugin gets a switch for free: a bool setting named "<id>_enabled", shown first in the
plugin's group on the suite's Settings page.
"""
from __future__ import annotations

import logging
from dataclasses import replace
from types import SimpleNamespace

from . import settings as S
from .settings import Field

log = logging.getLogger(__name__)


class Plugin:
    id = ""
    title = ""
    summary = ""
    fields: list[Field] = []
    tab: dict | None = None

    def __init__(self, settings: SimpleNamespace, ctx: SimpleNamespace):
        self.settings = settings        # this plugin's own settings, without the "<id>_" prefix
        self.ctx = ctx                  # what the module hands every plugin (mode, data folder, ...)

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def routes(self, router, ctx) -> None: ...
    def diagnose(self, d) -> None: ...

    def snapshot(self) -> dict:
        return {}

    def source(self) -> dict:
        return {}


class Plugins:
    """The plugins one module ships, which of them are on, and their combined hooks."""

    def __init__(self, classes: list[type[Plugin]], prefix: str):
        ids = [c.id for c in classes]
        if len(set(ids)) != len(ids) or not all(i.isidentifier() and i.islower() for i in ids):
            raise ValueError(f"plugin ids must be unique lower-case names: {ids}")
        for c in classes:
            stray = [f.name for f in c.fields if not f.name.startswith(c.id + "_") or f.name == c.id + "_enabled"]
            if stray:
                raise ValueError(f"settings of the {c.id} plugin must be named {c.id}_<something>: {stray}")
        self.classes, self.prefix = classes, prefix
        self.active: list[Plugin] = []

    # ------------------------------------------------------------------ settings
    def _fields(self, cls: type[Plugin]) -> list[Field]:
        """A plugin's settings as the module declares them: its switch first, all under one heading."""
        group = f"Plugin: {cls.title}"
        switch = Field(f"{cls.id}_enabled", kind="bool", default=False, label=f"Turn on the {cls.title.lower()}", group=group,
                       help=cls.summary)
        return [switch] + [replace(f, group=group) for f in cls.fields]

    def fields(self) -> list[Field]:
        """Every plugin's settings, to add to the module's own list."""
        return [f for c in self.classes for f in self._fields(c)]

    # ------------------------------------------------------------------ life cycle
    def load(self, ctx: SimpleNamespace, environ=None) -> "Plugins":
        """Create the plugins that are switched on, from the environment the module itself reads."""
        self.active = []
        for cls in self.classes:
            loaded = S.load(self._fields(cls), self.prefix, environ=environ)
            if not getattr(loaded, f"{cls.id}_enabled"):
                continue
            own = SimpleNamespace(**{k[len(cls.id) + 1:]: v for k, v in vars(loaded).items()})
            try:
                self.active.append(cls(own, ctx))
            except Exception:  # noqa: BLE001 - a plugin that cannot start must not take its module down
                log.exception("the %s plugin could not be set up; the module runs without it", cls.id)
        return self

    def start(self) -> None:
        for p in self.active:
            self._safely(p, "start")

    def stop(self) -> None:
        for p in self.active:
            self._safely(p, "stop")

    def _safely(self, plugin: Plugin, hook: str, *args):
        try:
            return getattr(plugin, hook)(*args)
        except Exception:  # noqa: BLE001 - one plugin's fault stays its own
            log.exception("the %s plugin failed in %s", plugin.id, hook)
            return None

    # ------------------------------------------------------------------ what the module asks
    def get(self, pid: str) -> Plugin | None:
        return next((p for p in self.active if p.id == pid), None)

    def describe(self) -> list[dict]:
        """Every plugin the module ships, and whether it is on. For the module's manifest and its page."""
        on = {p.id for p in self.active}
        return [{"id": c.id, "title": c.title, "summary": c.summary, "enabled": c.id in on, "tab": c.tab if c.id in on else None}
                for c in self.classes]

    def sources(self) -> dict:
        return {p.id: (self._safely(p, "source") or {}) for p in self.active}

    def diagnose(self, d) -> None:
        for p in self.active:
            self._safely(p, "diagnose", d)

    def mount(self, app, ctx: SimpleNamespace | None = None) -> None:
        """Add /api/v1/plugins, each active plugin's page data, and whatever routes it declares."""
        from fastapi import APIRouter, HTTPException

        @app.get("/api/v1/plugins")
        def plugins_list():
            return {"plugins": self.describe()}

        def page_of(plugin: Plugin):
            def page():
                data = self._safely(plugin, "snapshot")
                if data is None:
                    raise HTTPException(500, detail=f"the {plugin.title.lower()} could not describe itself; its log says why")
                return {"id": plugin.id, "title": plugin.title, **data}
            return page

        for p in self.active:
            router = APIRouter(prefix=f"/api/v1/plugins/{p.id}")
            router.add_api_route("", page_of(p), methods=["GET"])
            self._safely(p, "routes", router, ctx or p.ctx)
            app.include_router(router)
