# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""What the module remembers between readings: how busy each VM has been, and what changed.

Peaks are kept per day for two weeks, so sizing advice rests on the busiest a VM has been in that
time and not on one quiet moment. Changes are worked out by comparing each reading with the one
before: a host on the free licence does not serve its event history, and this needs none.

With a data folder set, both survive a restart; without one they start again.
"""
from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger(__name__)
DAY = 86400
KEEP_DAYS = 14
KEEP_CHANGES = 300
SAVE_EVERY = 300.0


class History:
    def __init__(self, data_dir: str = ""):
        self.path = os.path.join(data_dir, "history.json") if data_dir else ""
        self.first_seen: dict[str, float] = {}                 # VM moid -> when this module first saw it running
        self.days: dict[str, dict[str, list[float]]] = {}      # VM moid -> {day: [peak cpu share, peak active MB]}
        self.events: list[dict] = []                           # newest last
        self.started: float | None = None
        self._prev: dict[str, dict] | None = None
        self._saved = 0.0
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
            self.first_seen, self.days = d.get("firstSeen", {}), d.get("days", {})
            self.events, self.started, self._prev = d.get("events", []), d.get("started"), d.get("prev")
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as err:
            log.warning("cannot read %s: %s (starting fresh)", self.path, err)

    def save(self, force: bool = False) -> None:
        if not self.path or (not force and time.time() - self._saved < SAVE_EVERY):
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"firstSeen": self.first_seen, "days": self.days, "events": self.events[-KEEP_CHANGES:],
                           "started": self.started, "prev": self._prev}, fh)
            os.replace(tmp, self.path)
            self._saved = time.time()
        except OSError as err:
            log.warning("cannot write %s: %s", self.path, err)

    # ------------------------------------------------------------------ one reading
    def update(self, snap: dict, now: float) -> None:
        first = self._prev is None
        if self.started is None:
            self.started = now
        day = str(int(now // DAY))
        cur: dict[str, dict] = {}
        for v in snap["vms"]:
            on = v["powerState"] == "poweredOn"
            cur[v["id"]] = {"name": v["name"], "on": on, "cpus": v["numCpu"], "memMB": v["memMB"],
                            "snaps": sorted(s["name"] for s in v["snapshots"]), "disk": v["provisioned"],
                            "nics": sorted(f"{n['network']}:{int(n['connected'])}" for n in v["nics"]) if on else None}
            if not on:
                continue
            self.first_seen.setdefault(v["id"], now)
            p = v.get("perf") or {}
            cpu = (p["cpuUsagePct"] / 10000) if p.get("cpuUsagePct") is not None else \
                (v["quick"]["cpuMhz"] / v["maxCpuMhz"] if v["maxCpuMhz"] else 0.0)
            mem = (p["memActiveKb"] / 1024) if p.get("memActiveKb") is not None else v["quick"]["guestMemMB"]
            slot = self.days.setdefault(v["id"], {}).setdefault(day, [0.0, 0.0])
            slot[0], slot[1] = max(slot[0], round(min(1.0, cpu), 4)), max(slot[1], round(mem, 1))
        cutoff = int(now // DAY) - KEEP_DAYS
        for moid in list(self.days):
            self.days[moid] = {d: x for d, x in self.days[moid].items() if int(d) > cutoff}
            if moid not in cur:
                self.days.pop(moid, None)
                self.first_seen.pop(moid, None)
        if first:
            self._seed(snap, now)
        else:
            self._diff(self._prev, cur, now)
        self._prev = cur
        self.events = [e for e in self.events if now - e["at"] < 30 * DAY][-KEEP_CHANGES:]
        self.save()

    def _event(self, at: float, kind: str, moid: str, name: str, title: str, detail: str = "") -> None:
        self.events.append({"at": at, "kind": kind, "moid": moid, "name": name, "title": title, "detail": detail})

    def _seed(self, snap: dict, now: float) -> None:
        """The first reading has nothing to compare with: start from what the host itself dates."""
        if self.events:
            return
        for v in snap["vms"]:
            for s in v["snapshots"]:
                if s["created"] and now - s["created"] < 7 * DAY:
                    self._event(s["created"], "snapshot", v["id"], v["name"], f"Snapshot taken on {v['name']}", s["name"])
            if v["powerState"] == "poweredOn" and v["bootTime"] and now - v["bootTime"] < 2 * DAY:
                self._event(v["bootTime"], "power", v["id"], v["name"], f"{v['name']} was powered on")
        self.events.sort(key=lambda e: e["at"])

    def _diff(self, prev: dict, cur: dict, now: float) -> None:
        for moid, c in cur.items():
            p = prev.get(moid)
            name = c["name"]
            if p is None:
                self._event(now, "created", moid, name, f"{name} was added to the host", f"{c['cpus']} vCPU, {c['memMB'] // 1024} GB")
                continue
            if p["on"] != c["on"]:
                self._event(now, "power", moid, name, f"{name} was powered {'on' if c['on'] else 'off'}")
            if p["cpus"] != c["cpus"] or p["memMB"] != c["memMB"]:
                self._event(now, "resized", moid, name, f"{name} was resized",
                            f"{p['cpus']} vCPU, {p['memMB'] / 1024:g} GB → {c['cpus']} vCPU, {c['memMB'] / 1024:g} GB")
            if p.get("disk") and c["disk"] != p["disk"]:
                self._event(now, "resized", moid, name, f"{name}'s disks changed size",
                            f"{p['disk'] / 2**30:.0f} GB → {c['disk'] / 2**30:.0f} GB")
            added, gone = set(c["snaps"]) - set(p["snaps"]), set(p["snaps"]) - set(c["snaps"])
            if added:
                self._event(now, "snapshot", moid, name, f"Snapshot taken on {name}", ", ".join(sorted(added)))
            if gone:
                self._event(now, "snapshot", moid, name, f"Snapshot deleted on {name}", ", ".join(sorted(gone)))
            if p.get("nics") is not None and c.get("nics") is not None and p["nics"] != c["nics"]:
                self._event(now, "network", moid, name, f"{name}'s network adapters changed")
            if p["name"] != name:
                self._event(now, "renamed", moid, name, f"{p['name']} was renamed to {name}")
        for moid, p in prev.items():
            if moid not in cur:
                self._event(now, "removed", moid, p["name"], f"{p['name']} was removed from the host")

    # ------------------------------------------------------------------ what the builder reads
    def stats(self, moid: str) -> dict:
        days = self.days.get(moid) or {}
        if not days:
            return {"watchedS": 0}
        return {"peakCpuPct": max(x[0] for x in days.values()), "peakMemActiveMB": max(x[1] for x in days.values()),
                "watchedS": int(time.time() - self.first_seen.get(moid, time.time()))}

    def watched_s(self, now: float) -> int:
        return int(now - self.started) if self.started else 0

    def changes(self, now: float, vm_ids: dict[str, str], nodes: dict[str, dict]) -> list[dict]:
        """Newest first, in the shape the Triage page's "What changed" list reads."""
        out = []
        for e in reversed(self.events):
            nid = vm_ids.get(e["moid"], "")
            out.append({"ageS": int(max(0, now - e["at"])), "kind": e["kind"], "subject": nid if nid in nodes else "",
                        "namespace": nodes[nid]["namespace"] if nid in nodes else "", "name": e["name"], "title": e["title"],
                        "detail": e["detail"]})
        return out[:40]
