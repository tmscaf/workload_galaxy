# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Addresses you have chosen to block, and the list the router pulls.

Flow:
    you click Block  ->  an entry here (who, when, why, until when)
    the router's sync script fetches /api/v1/blocklist?token=..&have=<hash it applied>
                     ->  it validates every line again, loads the list into a pfBlockerNG table
                         (blocked in both directions), and reports the hash back on its next fetch
    so each entry knows when it actually took effect on the router.

The app holds no router credentials: the router reads from the app, never the other way round.
Every entry is checked here and again on the router, so a mistake (or a compromised app) cannot
block your own network, the router, private ranges or large parts of the internet.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

MAX_ENTRIES = 2000         # the router script refuses more than this too
MIN_PREFIX = 24            # nothing wider than a /24 can be blocked from here
EXPIRY_CHOICES = {"1d": 86400, "7d": 7 * 86400, "30d": 30 * 86400, "never": None}


class BlockError(ValueError):
    pass


def normalise(target: str, *, protected: list[str], allow_non_global: bool = False) -> str:
    """'203.0.113.9' or '203.0.113.0/24' -> canonical text, or BlockError saying why not."""
    text = (target or "").strip()
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError as err:
        raise BlockError(f"'{text}' is not an IP address or range") from err
    if net.version != 4:
        raise BlockError("only IPv4 addresses can be blocked from here")
    if net.prefixlen < MIN_PREFIX:
        raise BlockError(f"{net} is too wide: ranges larger than /{MIN_PREFIX} have to be blocked on the router itself")
    for p in protected:
        try:
            guard = ipaddress.ip_network(p, strict=False)
        except ValueError:
            continue                    # a malformed protect entry is skipped, never a reason to allow
        if guard.version == 4 and net.overlaps(guard):
            raise BlockError(f"{net} overlaps {p}, which is protected (your own network, router or a service it needs)")
    if not allow_non_global and not net.network_address.is_global:
        raise BlockError(f"{net} is a private or reserved address, not an internet one")
    return str(net.network_address) if net.prefixlen == 32 else str(net)


class BlockList:
    def __init__(self, data_dir: str = "", protected: list[str] | None = None, allow_non_global: bool = False):
        self.path = os.path.join(data_dir, "blocks.json") if data_dir else ""
        self.protected = protected or []
        self.allow_non_global = allow_non_global
        self.entries: dict[str, dict] = {}
        self.history: list[dict] = []           # removals and expiries, newest last (kept short)
        self.router = {"lastFetch": None, "appliedHash": None, "appliedAt": None, "fetchFrom": None, "rejected": None}
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.entries = {e["target"]: e for e in data.get("entries", [])}
            self.history = data.get("history", [])[-200:]
            self.router.update(data.get("router", {}))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError) as err:
            log.warning("block list unreadable (%s); starting empty", err)

    def _save(self) -> None:
        if not self.path:
            return
        payload = json.dumps({"entries": list(self.entries.values()), "history": self.history[-200:], "router": self.router})
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except OSError as err:
            log.warning("cannot save block list: %s", err)

    # ------------------------------------------------------------------ changes
    def add(self, target: str, *, reason: str, by: str, expires: str = "7d", finding: str = "", label: str = "",
            now: float | None = None) -> dict:
        now = now or time.time()
        key = normalise(target, protected=self.protected, allow_non_global=self.allow_non_global)
        if expires not in EXPIRY_CHOICES:
            raise BlockError(f"expiry must be one of {', '.join(EXPIRY_CHOICES)}")
        with self._lock:
            self._expire(now)
            if key not in self.entries and len(self.entries) >= MAX_ENTRIES:
                raise BlockError(f"the block list is full ({MAX_ENTRIES} entries); remove some first")
            ttl = EXPIRY_CHOICES[expires]
            entry = {"target": key, "label": (label or "")[:120], "reason": (reason or "")[:300], "finding": finding[:200],
                     "by": by[:60], "at": now, "expiresAt": now + ttl if ttl else None, "appliedAt": None}
            self.entries[key] = entry
            self._save()
            return dict(entry)

    def remove(self, target: str, *, by: str, now: float | None = None) -> dict:
        now = now or time.time()
        try:
            key = str(ipaddress.ip_network(target.strip(), strict=False))
            key = key[:-3] if key.endswith("/32") else key
        except ValueError as err:
            raise BlockError(f"'{target}' is not an IP address or range") from err
        with self._lock:
            entry = self.entries.pop(key, None)
            if not entry:
                raise BlockError(f"{key} is not on the block list")
            self.history.append({**entry, "removedAt": now, "removedBy": by[:60], "how": "removed"})
            self._save()
            return entry

    def _expire(self, now: float) -> None:
        gone = [k for k, e in self.entries.items() if e.get("expiresAt") and e["expiresAt"] <= now]
        for k in gone:
            self.history.append({**self.entries.pop(k), "removedAt": now, "removedBy": "", "how": "expired"})
        if gone:
            self._save()

    # ------------------------------------------------------------------ what the router reads
    def text(self, now: float | None = None) -> tuple[str, str]:
        """-> (list as the router reads it, its hash). One address or range per line."""
        now = now or time.time()
        with self._lock:
            self._expire(now)
            targets = sorted(self.entries, key=lambda t: ipaddress.ip_network(t, strict=False))
        body = "".join(t + "\n" for t in targets)
        return body, hashlib.sha256(body.encode()).hexdigest()[:16]

    def router_fetch(self, *, have: str | None, rejected: int | None, sender: str, now: float | None = None) -> None:
        """Record a fetch by the router. `have` is the hash of the list it currently has loaded."""
        now = now or time.time()
        _body, current = self.text(now)
        with self._lock:
            self.router.update(lastFetch=now, fetchFrom=sender)
            if rejected is not None:
                self.router["rejected"] = rejected
            if have and have != self.router.get("appliedHash"):
                self.router.update(appliedHash=have, appliedAt=now)
            if have and have == current:
                for e in self.entries.values():
                    if not e.get("appliedAt"):
                        e["appliedAt"] = now
            self._save()

    # ------------------------------------------------------------------ reading
    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        body, current = self.text(now)
        with self._lock:
            entries = sorted((dict(e) for e in self.entries.values()), key=lambda e: -e["at"])
            r = dict(self.router)
        in_sync = bool(r.get("appliedHash")) and r["appliedHash"] == current
        return {"entries": entries, "count": len(entries), "hash": current, "inSync": in_sync,
                "router": {**r, "lastFetchS": round(now - r["lastFetch"]) if r.get("lastFetch") else None},
                "history": list(reversed(self.history[-30:])), "max": MAX_ENTRIES, "minPrefix": MIN_PREFIX,
                "expiryChoices": list(EXPIRY_CHOICES)}

    def lookup(self) -> dict[str, dict]:
        """target -> entry, for marking things on the map. Ranges are matched by the caller."""
        with self._lock:
            return {k: dict(e) for k, e in self.entries.items()}

    def covering(self, ip: str, table: dict[str, dict] | None = None) -> dict | None:
        table = self.lookup() if table is None else table
        if ip in table:
            return table[ip]
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return None
        for k, e in table.items():
            if "/" in k and a in ipaddress.ip_network(k, strict=False):
                return e
        return None
