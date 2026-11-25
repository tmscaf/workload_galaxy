"""Ask each module how it is doing, in the background, so the selector can show it at a glance.

The shell's server does the asking, not the browser: modules live on other addresses, and a page
may not read another site's API. Only two small reads per module, both from its public API.
"""
from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)
TIMEOUT = 4.0


def _get(url: str, verify: bool) -> dict:
    ctx = None if verify else ssl._create_unverified_context()
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "workload-galaxy-shell"})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as res:      # noqa: S310 - http(s) only, checked in config
        return json.loads(res.read(4_000_000).decode("utf-8", "replace"))


def call(module: dict, method: str, path: str, *, body: dict | None = None, token: str = "", verify: bool = False,
         timeout: float = 20.0) -> tuple[int, dict]:
    """One request to a module's API on the shell's behalf -> (status, JSON body). A module that cannot
    be reached is (0, {"detail": why})."""
    ctx = None if verify else ssl._create_unverified_context()
    headers = {"Accept": "application/json", "User-Agent": "workload-galaxy-shell"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(module["probeUrl"] + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as res:      # noqa: S310 - http(s) only, checked in config
            return res.status, json.loads(res.read(4_000_000).decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as err:
        try:
            payload = json.loads(err.read(200_000).decode("utf-8", "replace") or "{}")
        except ValueError:
            payload = {}
        return err.code, payload if isinstance(payload, dict) else {"detail": str(payload)}
    except (urllib.error.URLError, OSError, ValueError) as err:
        reason = getattr(err, "reason", err)
        return 0, {"detail": (getattr(reason, "strerror", None) or str(reason) or "no answer").lower()}


def check(module: dict, verify: bool = False) -> dict:
    """One module's status. Never raises: a module that is down is a status, not an error."""
    out = {"ok": False, "error": None, "version": "", "mode": "", "name": "", "problems": None, "critical": None,
           "level": "off", "stale": None}
    base = module["probeUrl"]
    try:
        meta = _get(base + "api/v1/meta", verify)
    except urllib.error.HTTPError as err:
        out["error"] = f"answered {err.code}"
        return out
    except (urllib.error.URLError, OSError, ValueError) as err:
        reason = getattr(err, "reason", err)
        text = (getattr(reason, "strerror", None) or str(reason) or "no answer").lower()
        out["error"] = "it took too long to answer" if "timed out" in text else text
        return out
    out.update(ok=True, version=str(meta.get("version") or ""), mode=str(meta.get("mode") or ""),
               name=str((meta.get("cluster") or {}).get("name") or (meta.get("estate") or {}).get("name") or ""),
               stale=meta.get("error") or None, level="ok")
    try:
        summary = (_get(base + "api/v1/triage?severity=critical", verify).get("summary") or {})
        out["problems"], out["critical"] = int(summary.get("problems") or 0), int(summary.get("problemsCritical") or 0)
        out["level"] = "critical" if out["critical"] else "warning" if out["problems"] else "ok"
    except (urllib.error.URLError, OSError, ValueError) as err:
        log.debug("no triage summary from %s: %s", module["id"], err)        # still loading its first reading
    return out


class Prober:
    def __init__(self, modules: list[dict], seconds: int = 20, verify: bool = False, checker=check):
        self.modules, self.seconds, self.verify, self._check = modules, seconds, verify, checker
        self.status: dict[str, dict] = {}
        self.checked_at = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="probe", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.seconds)

    def refresh(self) -> None:
        if not self.modules:
            return
        with ThreadPoolExecutor(max_workers=min(8, len(self.modules))) as pool:
            results = list(pool.map(lambda m: self._check(m, self.verify), self.modules))
        with self._lock:
            self.status = {m["id"]: r for m, r in zip(self.modules, results)}
            self.checked_at = time.time()

    def snapshot(self) -> list[dict]:
        with self._lock:
            status, at = dict(self.status), self.checked_at
        blank = {"ok": None, "error": None, "version": "", "mode": "", "name": "", "problems": None, "critical": None,
                 "level": "off", "stale": None}
        return [{"id": m["id"], "title": m["title"], "summary": m["summary"], "url": m["url"],
                 **status.get(m["id"], blank), "checkedS": round(time.time() - at) if at else None} for m in self.modules]
