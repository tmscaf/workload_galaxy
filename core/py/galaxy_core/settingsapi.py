"""The settings API a module offers the suite, and two ways to apply what is saved.

    GET /api/v1/settings        the form: fields, current values, where each value comes from
    PUT /api/v1/settings        save, and start using what was saved
    POST /api/v1/settings/test  try values without saving them

All three need the settings token the module shares with the suite (`Authorization: Bearer ...`).
Without a token configured, the module says what it takes but not what it has, and refuses changes.

A module built on galaxy_core.webapp swaps in a new reader when settings are saved. A module that
reads its environment once at start instead uses `boot()` and `RestartingSettings`: saved values are
laid over the environment before the module loads, and saving restarts the process in place.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
import time

from fastapi import FastAPI, HTTPException, Request

from .settings import Field, SettingsError, Store

log = logging.getLogger(__name__)


def attach(app: FastAPI, *, api, token: str, manifest: dict, mode) -> None:
    """Add the three routes. `api` has describe() -> dict, apply(values) -> dict and test(values) -> dict;
    `mode` is a callable giving the module's current data source ("live", "sample", ...)."""
    token = token or ""

    def allowed(request: Request) -> None:
        if not token:
            raise HTTPException(403, detail="this module takes its settings from where it is deployed (no settings token is set)")
        sent = request.headers.get("authorization", "")
        given = sent[7:] if sent.lower().startswith("bearer ") else ""
        if not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, detail="the settings token is missing or wrong")

    async def values(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, detail="send JSON") from None
        return (body or {}).get("values") if isinstance(body, dict) and "values" in body else body

    head = {"module": {"id": manifest.get("id"), "title": manifest.get("title")}}

    @app.get("/api/v1/settings")
    def settings(request: Request):
        # Current values (addresses, account names) are for the suite only. Without a token the
        # module says what it takes, not what it has.
        desc = api.describe()
        if token:
            allowed(request)
        else:
            desc["fields"] = [{**f, "value": None, "set": None, "source": None} for f in desc["fields"]]
            desc["missing"] = []
        return {**head, "writable": bool(token), "mode": mode(), **desc}

    @app.put("/api/v1/settings")
    async def save_settings(request: Request):
        allowed(request)
        try:
            result = api.apply(await values(request))
        except SettingsError as err:
            raise HTTPException(400, detail=str(err)) from err
        return {"saved": True, **result, **head, "writable": True, "mode": mode(), **api.describe()}

    @app.post("/api/v1/settings/test")
    async def test_settings(request: Request):
        allowed(request)
        try:
            return api.test(await values(request))
        except SettingsError as err:
            raise HTTPException(400, detail=str(err)) from err


# ---------------------------------------------------------------------------------------------- restart-to-apply
def boot(fields: list[Field], prefix: str) -> tuple[Store, dict]:
    """Call before a module reads its environment. Lays the saved settings over os.environ and
    returns (the store, the environment as the deployment set it)."""
    base = {k: v for k, v in os.environ.items()}
    carry = f"{prefix}__SAVED_SETTINGS"
    data_dir = base.get(f"{prefix}_DATA_DIR", "")
    store = Store(fields, prefix, os.path.join(data_dir, "settings.json") if data_dir else "", environ=base, carry=carry)
    os.environ.update(store.as_environment())
    os.environ.pop(carry, None)
    return store, base


def restart_in_place(env: dict, *, delay: float = 0.8, before=None) -> None:
    """Start this same program again, with this environment, shortly after the current request has
    been answered. The process keeps its id, so nothing outside has to restart it."""
    def go():
        time.sleep(delay)
        try:
            if before:
                before()
        except Exception:  # noqa: BLE001 - the restart matters more than a clean stop
            log.exception("stopping before the restart failed")
        log.info("restarting to apply the saved settings")
        sys.stdout.flush()
        sys.stderr.flush()
        os.execve(sys.executable, list(getattr(sys, "orig_argv", None) or [sys.executable] + sys.argv), env)
    threading.Thread(target=go, name="restart", daemon=True).start()


class RestartingSettings:
    """Settings for a module that reads its environment once: saving restarts it in place."""

    def __init__(self, store: Store, base_env: dict, *, tester=None, before_restart=None, restart=None,
                 sample_modes: tuple = ("sample", "demo")):
        self.store, self.base_env = store, base_env
        self._tester, self._before, self._restart, self._sample = tester, before_restart, restart, sample_modes

    def describe(self) -> dict:
        d = self.store.describe()
        if str(getattr(self.store.effective(), "mode", "")).lower() in self._sample:
            d["missing"] = []                     # made-up data needs nothing
        d["appliesBy"] = "restart"
        return d

    def test(self, values: dict) -> dict:
        candidate = self.store.preview(values or {})
        if str(getattr(candidate, "mode", "")).lower() in self._sample:
            return {"ok": True, "message": "Made-up data needs no connection."}
        if self._tester is None:
            return {"ok": True, "message": "Nothing to test for these settings."}
        return self._tester(candidate)

    def apply(self, values: dict) -> dict:
        forgot = self.store.update(values or {})
        env = dict(self.base_env)
        if not self.store.path:                   # no data folder: hand the settings to the next run
            env[self.store.carry] = json.dumps(self.store.saved)
        (self._restart or restart_in_place)(env, before=self._before)
        return {"forgot": forgot, "restarting": True}
