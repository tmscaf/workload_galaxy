# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""The module's refresh loop: read the host, remember, build the graph."""
from __future__ import annotations

import logging
import time

from galaxy_core.state import State

from .graph import Builder
from .history import History
from .vsphere import Vsphere, VsphereError

log = logging.getLogger(__name__)
EMPTY = {"about": {}, "hosts": [], "datastores": [], "vms": [], "tasks": [], "warnings": []}


def _vm_cols(n: dict) -> dict:
    """The columns the virtual machine table sorts and shows."""
    v = n.get("vm") or {}
    return {"on": bool(v.get("on")), "os": v.get("os", ""), "ip": v.get("ip", ""), "cpus": v.get("cpus", 0), "memMB": v.get("memMB", 0),
            "cpuPct": v.get("cpuPct") if v.get("on") else -1, "readyPct": v.get("readyPct") if v.get("readyPct") is not None else -1,
            "memActiveMB": v.get("memActiveMB", 0) if v.get("on") else -1,
            "memActivePct": v.get("memActivePct") if v.get("memActivePct") is not None else -1,
            "committed": v.get("committed", 0), "provisioned": v.get("provisioned", 0),
            "net": (v.get("netRxBps", 0) + v.get("netTxBps", 0)) if v.get("on") else -1,
            "disk": (v.get("diskReadBps", 0) + v.get("diskWriteBps", 0)) if v.get("on") else -1,
            "snapshots": v.get("snapshotCount", 0), "snapshotBytes": v.get("snapshotBytes", 0),
            "tools": v.get("toolsLabel", ""), "uptime": v.get("uptimeS") if v.get("uptimeS") is not None else -1,
            "datastores": ", ".join(v.get("datastores", [])), "networks": ", ".join(v.get("networks", []))}


def _search(n: dict) -> dict:
    v = n.get("vm") or {}
    return {"contains": [v.get("hostname", ""), v.get("os", "")] + [x.get("mac", "") for x in v.get("nics", [])],
            "prefix": v.get("ips", []), "hit": {"ip": v.get("ip", "")}}


class EsxiState(State):
    index_options = {"workload_cols": staticmethod(_vm_cols), "search_extra": staticmethod(_search),
                     "text_fields": ("os", "ip", "datastores", "networks", "tools")}

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.refresh_seconds = settings.refresh_seconds
        self.history = History(settings.data_dir)
        self.sample = None
        self.client: Vsphere | None = None
        if settings.mode == "sample":
            from .sample import SampleHost
            self.sample = SampleHost()
        elif settings.host:
            self.client = Vsphere(settings.host, settings.user, settings.password, port=settings.port, verify=settings.verify_tls)
        self._snap: dict | None = None
        self._snap_at = 0.0

    def on_stop(self) -> None:
        self.history.save(force=True)
        if self.client:
            self.client.close()

    def _read(self, now: float) -> str | None:
        """Fetch a new reading. Returns an error text when the host failed (the old reading stays)."""
        if self.sample:
            self._snap, self._snap_at = self.sample.snapshot(now), now
            if not self.history.days:           # the sample has "been watched" for a while, so sizing advice shows
                self.history.started = now - 6 * 3600
                for v in self._snap["vms"]:
                    self.history.first_seen[v["id"]] = now - 6 * 3600
            return None
        if not self.client:
            return "no host configured (set ESXIGALAXY_HOST, ESXIGALAXY_USER and ESXIGALAXY_PASSWORD)"
        try:
            self._snap, self._snap_at = self.client.snapshot(), now
        except VsphereError as err:
            return str(err)
        return None

    def build(self) -> tuple[dict, str | None]:
        now, s = time.time(), self.settings
        error = self._read(now)
        snap = self._snap or EMPTY
        if error is None:
            self.history.update(snap, now)
        sources = {
            "mode": s.mode,
            "vsphere": {"enabled": bool(self.client or self.sample), "error": error, "host": s.host,
                        "fetchSeconds": self.client.fetch_seconds if self.client else 0.0, "vms": len(snap["vms"]),
                        "hosts": len(snap["hosts"]), "ageS": round(now - self._snap_at, 1) if self._snap_at else None,
                        "product": snap["about"].get("fullName", ""), "warnings": snap.get("warnings", [])},
            "history": {"persistent": bool(self.history.path), "watchedS": self.history.watched_s(now)},
        }
        graph = Builder(s, snap, history=self.history, sources=sources, now=now).build()
        return graph, error
