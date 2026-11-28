# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""The ad blocker: one list of ad and tracker names, published for the router's DNS blocking to pull.

Flow:
    Settings names the block lists to use (addresses of public lists), plus names of your own
    to always block or never block
                     ->  this plugin fetches each list, keeps only what is a plain host name,
                         joins them, takes out what must never be blocked, and publishes the result
    the router's DNS blocking (pfBlockerNG) fetches /api/v1/plugins/adblock/feed?token=...
                     ->  on its own schedule, as it does with any other list it is given

The plugin holds no router credentials and never connects to the router. It also bundles no
block list: each one is fetched from its publisher while the plugin runs, and stays under that
publisher's licence.

What can go wrong is kept from reaching the router:
    - a list that cannot be fetched keeps its last good copy (on disk, if there is a data folder)
    - while a list has no good copy yet, the feed answers 503 for the first minutes of a run, so
      the router keeps what it already has instead of loading a short list
    - names for your own network (.lan, .local, .home.arpa, ...), bare words and addresses are
      never published, whatever a list says
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import PlainTextResponse

from galaxy_core.plugins import Plugin
from galaxy_core.settings import Field

from ..util import ago, plural

log = logging.getLogger(__name__)

MAX_LIST_BYTES = 40 * 1024 * 1024       # no single list may be larger than this
RETRY_SECONDS = 600                     # a list that failed is tried again this soon
HOLD_SECONDS = 900                      # how long the feed is held back for a list with no good copy yet
PULL_OVERDUE_SECONDS = 26 * 3600        # the router pulls on its own schedule; a day without one is worth a note
LABEL = re.compile(r"^(?!-)[a-z0-9_-]{1,63}(?<!-)$")
COMMENT = re.compile(r"(^|\s)#.*$")
# Never published: names that belong to someone's own network, or that are not real internet names.
LOCAL_SUFFIXES = ("local", "localhost", "localdomain", "lan", "home", "internal", "intranet", "corp", "arpa", "test", "invalid",
                  "example", "onion")
HOSTS_ADDRESSES = {"0.0.0.0", "127.0.0.1", "::", "::1", "0", "255.255.255.255"}


# ---------------------------------------------------------------------------------------------- names
def why_not(name: str) -> str | None:
    """Why this cannot be published as a blocked name, or None when it can."""
    if not name or len(name) > 253:
        return "not a host name"
    labels = name.split(".")
    if len(labels) < 2:
        return "a bare word, not a full name"
    if not all(LABEL.match(x) for x in labels):
        return "not a host name"
    if labels[-1].isdigit():
        return "an address, not a name"
    if labels[-1] in LOCAL_SUFFIXES:
        return f"names ending in .{labels[-1]} belong to a local network"
    return None


def clean(raw: str) -> str | None:
    """A name as a list wrote it -> the name to publish, or None."""
    name = raw.strip().strip(".").lower()
    return None if why_not(name) else name


def parse_list(text: str) -> set[str]:
    """Names out of a block list in any of the three usual shapes: a hosts file ("0.0.0.0 name"),
    one name per line, or ad-blocker rules ("||name^"). Anything else on a line is ignored."""
    out: set[str] = set()
    for line in text.splitlines():
        # A "#" starts a comment only at the start of a line or after a space. "name##.banner" is an
        # ad-blocker rule about part of a page on that site, and must never be read as "block name".
        line = COMMENT.sub("", line).strip()
        if not line or line[0] in "!([":
            continue
        if line.startswith("||"):
            body = line[2:]
            if not body.endswith("^") or any(c in body[:-1] for c in "/*$^|"):
                continue                               # a rule about a path or with options: not a plain name
            words = [body[:-1]]
        else:
            words = line.split()
            if words[0] in HOSTS_ADDRESSES:
                words = words[1:]
            elif len(words) > 1:
                continue                               # not a shape we know
        for w in words:
            name = clean(w)
            if name:
                out.add(name)
    return out


def own_names(text: str) -> tuple[list[str], list[dict]]:
    """Names typed into Settings -> (usable names, [{name, why}] for the ones that are not)."""
    good, bad = [], []
    for raw in re.split(r"[\s,]+", text or ""):
        if not raw:
            continue
        name = raw.strip().strip(".").lower()
        name = name[2:] if name.startswith("*.") else name
        why = why_not(name)
        if why:
            bad.append({"name": raw[:80], "why": why})
        elif name not in good:
            good.append(name)
    return good, bad


def allowed(name: str, allow: set[str]) -> bool:
    """True when the name, or a name above it, is on the never-block list."""
    if not allow:
        return False
    if name in allow:
        return True
    cut = name.find(".")
    while cut != -1:
        if name[cut + 1:] in allow:
            return True
        cut = name.find(".", cut + 1)
    return False


def list_urls(text: str) -> tuple[list[str], list[dict]]:
    good, bad = [], []
    for raw in (text or "").split():
        parts = urlsplit(raw)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            bad.append({"name": raw[:120], "why": "not a web address (it must start with https:// or http://)"})
        elif raw not in good:
            good.append(raw)
    return good, bad


def short(url: str) -> str:
    parts = urlsplit(url)
    tail = parts.path.rstrip("/").rsplit("/", 2)[-2:]
    return f"{parts.netloc}/…/{'/'.join(tail)}" if parts.path.count("/") > 2 else f"{parts.netloc}{parts.path}"


# ---------------------------------------------------------------------------------------------- sample
SAMPLE_LISTS = {
    "https://lists.example.net/ads/hosts": ["ads.example.net", "banners.example.net", "pixel.adnetwork.example.com",
                                            "static.adnetwork.example.com", "track.example.org", "metrics.shop.example.com"],
    "https://lists.example.org/trackers.txt": ["track.example.org", "telemetry.tv.example.com", "beacon.example.net",
                                               "stats.phone.example.org"],
}


def fetch_http(url: str, timeout: float = 45.0) -> str:
    """Download one list. Raises OSError-like errors with a message a person can read."""
    import httpx
    size, chunks = 0, []
    try:
        with httpx.stream("GET", url, timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": "workload-galaxy-adblock", "Accept": "text/plain, */*"}) as resp:
            if resp.status_code != 200:
                raise OSError(f"the list's server answered {resp.status_code}")
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_LIST_BYTES:
                    raise OSError(f"the list is larger than {MAX_LIST_BYTES // (1024 * 1024)} MB")
                chunks.append(chunk)
    except httpx.HTTPError as err:
        raise OSError(f"could not be fetched ({type(err).__name__}: {err})") from err
    return b"".join(chunks).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------------------------- the plugin
class AdBlocker(Plugin):
    id = "adblock"
    title = "Ad blocker"
    summary = ("Publishes one list of ad and tracker names for the router's DNS blocking to pull, built from the block lists "
               "you choose plus your own names, and shows which devices are being blocked.")
    tab = {"title": "Ad blocker", "hint": "The ad and tracker names being published for the router to block, where they come "
                                          "from, and which devices are running into them"}
    fields = [
        Field("adblock_lists", kind="text", label="Block lists",
              help="Web addresses of block lists, one per line. Each is fetched from its publisher and stays under its "
                   "publisher's licence; nothing is bundled. A widely used general one: "
                   "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"),
        Field("adblock_block", kind="text", label="Always block", help="Names of your own to block, one per line."),
        Field("adblock_allow", kind="text", label="Never block",
              help="Names that must keep working, one per line. Covers the name and everything under it, and wins over every list."),
        Field("adblock_refresh_hours", kind="int", default=24, minimum=1, label="Fetch the lists every (hours)", advanced=True),
        Field("adblock_max_names", kind="int", default=300000, minimum=1000, label="Most names to publish", advanced=True,
              help="A small router has limited memory for DNS blocking. Above this, list names are left out and a finding says so."),
    ]

    def __init__(self, settings, ctx, fetch=fetch_http):
        super().__init__(settings, ctx)
        self.sample = ctx.mode == "sample"
        self.fetch = fetch
        self.started = time.time()
        self.urls, self.bad_urls = list_urls(settings.lists)
        self.block, bad_block = own_names(settings.block)
        self.allow, bad_allow = own_names(settings.allow)
        self.rejected = self.bad_urls + bad_block + bad_allow
        if self.sample:
            self.urls = list(SAMPLE_LISTS)
        self.dir = os.path.join(ctx.data_dir, "adblock") if ctx.data_dir and not self.sample else ""
        # url -> {names: set | None, fetchedAt, triedAt, error, fromCache}
        self.lists: dict[str, dict] = {u: {"names": None, "fetchedAt": None, "triedAt": None, "error": None, "fromCache": False}
                                      for u in self.urls}
        self.router = {"lastPull": None, "from": None, "hash": None}
        self._feed: tuple[str, str] | None = None          # (text, hash)
        self._stats = {"count": 0, "removedByAllow": 0, "truncated": 0, "builtAt": None}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._load()
        if self.sample:
            now = time.time()
            for url, names in SAMPLE_LISTS.items():
                self.lists[url].update(names=set(names), fetchedAt=now - 3 * 3600, triedAt=now - 3 * 3600)
            self._build()
            self.router.update(lastPull=now - 20 * 60, **{"from": "the sample router"}, hash=self._feed[1])
        elif any(st["names"] is not None for st in self.lists.values()) or not self.urls:
            self._build()

    # ------------------------------------------------------------------ what survives a restart
    def _cache(self, url: str) -> str:
        return os.path.join(self.dir, hashlib.sha256(url.encode()).hexdigest()[:20] + ".txt")

    def _load(self) -> None:
        if not self.dir:
            return
        try:
            with open(os.path.join(self.dir, "state.json"), encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            saved = {}
        self.router.update({k: v for k, v in (saved.get("router") or {}).items() if k in self.router})
        for url, st in self.lists.items():
            try:
                with open(self._cache(url), encoding="utf-8") as fh:
                    st["names"] = {line.strip() for line in fh if line.strip()}
                st["fetchedAt"] = (saved.get("lists") or {}).get(url) or os.path.getmtime(self._cache(url))
                st["fromCache"] = True
            except OSError:
                continue

    def _save(self, url: str | None = None) -> None:
        if not self.dir:
            return
        try:
            os.makedirs(self.dir, exist_ok=True)
            if url and self.lists[url]["names"] is not None:
                tmp = self._cache(url) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(sorted(self.lists[url]["names"])) + "\n")
                os.replace(tmp, self._cache(url))
            tmp = os.path.join(self.dir, "state.json.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"router": self.router, "lists": {u: st["fetchedAt"] for u, st in self.lists.items()}}, fh)
            os.replace(tmp, os.path.join(self.dir, "state.json"))
        except OSError as err:
            log.warning("cannot save the ad blocker's lists: %s", err)

    # ------------------------------------------------------------------ fetching and building
    def start(self) -> None:
        if not self.sample:
            threading.Thread(target=self._loop, name="adblock", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:  # noqa: BLE001 - keep publishing the last good list
                log.exception("refreshing the block lists failed")
            self._stop.wait(60)

    def _due(self, st: dict, now: float) -> bool:
        if st["triedAt"] is None:
            return True
        if st["error"]:
            return now - st["triedAt"] >= RETRY_SECONDS
        return now - (st["fetchedAt"] or 0) >= self.settings.refresh_hours * 3600

    def refresh(self, now: float | None = None) -> bool:
        """Fetch every list that is due. Returns whether anything changed."""
        now = now or time.time()
        changed = False
        for url, st in self.lists.items():
            if self._stop.is_set() or not self._due(st, now):
                continue
            st["triedAt"] = now
            try:
                names = parse_list(self.fetch(url))
                if not names:
                    raise OSError("it holds no host names (is this the address of a block list?)")
            except OSError as err:
                st["error"] = str(err)
                log.warning("block list %s: %s", url, err)
                changed = True
                continue
            st.update(names=names, fetchedAt=now, error=None, fromCache=False)
            self._save(url)
            changed = True
            log.info("block list %s: %d names", url, len(names))
        if changed or self._feed is None:
            self._build(now)
        return changed

    def _build(self, now: float | None = None) -> None:
        now = now or time.time()
        allow = set(self.allow)
        from_lists: set[str] = set()
        for st in self.lists.values():
            if st["names"]:
                from_lists |= st["names"]
        own = [n for n in self.block if not allowed(n, allow)]
        kept = sorted(n for n in from_lists if not allowed(n, allow))
        removed = len(from_lists) - len(kept) + (len(self.block) - len(own))
        room = max(0, self.settings.max_names - len(own))
        truncated = max(0, len(kept) - room)
        names = sorted(set(own) | set(kept[:room]))
        text = "".join(n + "\n" for n in names)
        with self._lock:
            self._feed = (text, hashlib.sha256(text.encode()).hexdigest()[:16])
            self._stats = {"count": len(names), "removedByAllow": removed, "truncated": truncated, "builtAt": now}

    # ------------------------------------------------------------------ what the router reads
    def waiting_for(self, now: float | None = None) -> list[str]:
        """Lists with no good copy yet, while it is still early enough that holding the feed back is right."""
        now = now or time.time()
        if now - self.started > HOLD_SECONDS:
            return []
        return [u for u, st in self.lists.items() if st["names"] is None]

    def feed(self, now: float | None = None) -> tuple[str, str] | None:
        """-> (one name per line, its hash), or None while there is nothing safe to hand the router."""
        with self._lock:
            feed = self._feed
        if feed is None or not feed[0] or self.waiting_for(now):
            return None
        return feed

    def pulled(self, sender: str, digest: str, now: float | None = None) -> None:
        self.router.update(lastPull=now or time.time(), **{"from": sender}, hash=digest)
        self._save()

    def routes(self, router, ctx) -> None:
        @router.get("/feed", include_in_schema=False)
        def feed(request: Request, token: str = ""):
            """The list the router's DNS blocking pulls: one name per line."""
            sender = ctx.router_only(request, token)
            ready = self.feed()
            if ready is None:
                raise HTTPException(503, detail="the list is not ready yet; keep the one you have", headers={"Retry-After": "300"})
            self.pulled(sender, ready[1])
            return PlainTextResponse(ready[0], headers={"X-Feed-Hash": ready[1], "Cache-Control": "no-store"})

    # ------------------------------------------------------------------ what people read
    def _blocked(self) -> dict:
        graph = self.ctx.graph() or {}
        rows = [{"id": n["id"], "name": n["name"], "ip": n["device"].get("ip", ""), "hits": n["device"].get("dnsBlocked", 0)}
                for n in graph.get("nodes", []) if n.get("kind") == "workload" and (n.get("device") or {}).get("dnsBlocked")]
        rows.sort(key=lambda r: -r["hits"])
        return {"counted": bool(self.ctx.counts_blocked), "total": sum(r["hits"] for r in rows), "devices": rows[:15],
                "moreDevices": max(0, len(rows) - 15)}

    def snapshot(self, now: float | None = None) -> dict:
        now = now or time.time()
        with self._lock:
            feed, stats = self._feed, dict(self._stats)
        waiting = self.waiting_for(now)
        ready = self.feed(now) is not None
        lists = []
        for url, st in self.lists.items():
            age = round(now - st["fetchedAt"]) if st["fetchedAt"] else None
            lists.append({"url": url, "short": short(url), "names": len(st["names"]) if st["names"] is not None else None,
                          "ageS": age, "error": st["error"], "fromCache": st["fromCache"],
                          "stale": bool(st["error"] and st["names"] is not None)})
        r = self.router
        return {"mode": self.ctx.mode, "ready": ready, "waitingFor": [short(u) for u in waiting], "count": stats["count"],
                "hash": feed[1] if feed else None, "builtS": round(now - stats["builtAt"]) if stats["builtAt"] else None,
                "removedByAllow": stats["removedByAllow"], "truncated": stats["truncated"], "max": self.settings.max_names,
                "lists": lists, "refreshHours": self.settings.refresh_hours,
                "own": {"block": self.block, "allow": self.allow, "rejected": self.rejected},
                "router": {"configured": bool(self.ctx.router_configured) or self.sample,
                           "lastPullS": round(now - r["lastPull"]) if r["lastPull"] else None, "from": r["from"],
                           "current": (r["hash"] == feed[1]) if r["hash"] and feed else None},
                "feedPath": f"api/v1/plugins/{self.id}/feed", "blocked": self._blocked()}

    def source(self) -> dict:
        snap = self.snapshot()
        return {"enabled": True, "names": snap["count"], "ready": snap["ready"], "lists": len(snap["lists"]),
                "listErrors": sum(1 for x in snap["lists"] if x["error"]), "routerPullS": snap["router"]["lastPullS"]}

    # ------------------------------------------------------------------ findings
    def diagnose(self, d, now: float | None = None) -> None:
        now = now or time.time()
        snap = self.snapshot(now)
        where = "Settings > Network > Plugin: Ad blocker"
        if not snap["lists"] and not self.block:
            d.add("platform", "adblock-empty", "The ad blocker is on but has nothing to block", severity="info",
                  detail="No block list is set and there are no names of your own.",
                  cause="The plugin bundles no list; it publishes what you point it at.",
                  fix=f"Add a block list's address under {where}.")
        for row in snap["lists"]:
            if not row["error"]:
                continue
            never = row["names"] is None
            d.add("platform", f"adblock-list:{row['short']}",
                  f"A block list {'has never been fetched' if never else 'could not be refreshed'}: {row['short']}",
                  severity="warning" if never else "info", detail=f"{row['url']}: {row['error']}.",
                  cause="The address is wrong, the publisher's site is down, or this module cannot reach the internet." if never
                  else f"The publisher's site is down or unreachable. The copy from {ago(row['ageS'])} ago is still being published.",
                  fix=f"Open the address in a browser to check it, and correct it under {where}." if never
                  else "Nothing to do unless it keeps failing; it is tried again every 10 minutes.")
        for bad in snap["own"]["rejected"]:
            d.add("platform", f"adblock-entry:{bad['name']}", f"An ad blocker setting was left out: {bad['name']}", severity="info",
                  detail=f"{bad['name']}: {bad['why']}.", cause="What was typed cannot be used as it is.", fix=f"Correct it under {where}.")
        if snap["truncated"]:
            d.add("platform", "adblock-full", f"The block list is full: {plural(snap['truncated'], 'name')} left out", severity="warning",
                  detail=f"The lists hold more than the {snap['max']:,} names this plugin is set to publish.",
                  cause="Several large lists were added, and most of their names overlap less than expected.",
                  fix=f"Remove a list, or raise \"Most names to publish\" under {where} if the router has the memory for it.")
        if self.sample or not snap["count"]:
            return
        r = snap["router"]
        if not r["configured"]:
            d.add("platform", "adblock-router", "The router is not set up to pull the ad blocker's list", severity="info",
                  detail=f"{snap['count']:,} names are ready, and nothing is fetching them.",
                  cause="The router token is not set, so the list is not being served.",
                  fix="Set the Router token under Settings > Network > Blocking, then add the list to the router's DNS blocking "
                      "(the Network module's README has the steps).")
        elif r["lastPullS"] is None and now - self.started > 2 * 3600:
            d.add("platform", "adblock-router", "The router has never pulled the ad blocker's list", severity="warning",
                  detail=f"{snap['count']:,} names are ready. No fetch has arrived since this module started {ago(now - self.started)} ago.",
                  cause="The list has not been added to the router's DNS blocking, or its address or token there is wrong.",
                  fix="On the router, add the list's address as a DNS block list (the Network module's README has the steps).")
        elif r["lastPullS"] is not None and r["lastPullS"] > PULL_OVERDUE_SECONDS:
            d.add("platform", "adblock-router", f"The router last pulled the ad blocker's list {ago(r['lastPullS'])} ago", severity="warning",
                  detail="It normally fetches at least once a day." + ("" if r["current"] else " The list has changed since then."),
                  cause="The router's DNS blocking is stopped, its update schedule was turned off, or it can no longer reach this module.",
                  fix="On the router, check that DNS blocking is running and run its update by hand to see the error.")
