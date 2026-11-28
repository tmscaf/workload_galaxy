# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""The ad blocker plugin: what a block list may contribute, what is never published, and the feed the router pulls."""
import importlib
from types import SimpleNamespace

import pytest

from netgalaxy.plugins import PLUGINS
from netgalaxy.plugins.adblock import HOLD_SECONDS, AdBlocker, allowed, own_names, parse_list

HOSTS = """
# a hosts-format list
127.0.0.1 localhost
0.0.0.0 0.0.0.0
0.0.0.0 ads.example.net   # trailing comment
0.0.0.0 Tracker.Example.org. pixel.example.org
:: beacon.example.net
"""
RULES = """
! an ad-blocker rule list
[Adblock Plus 2.0]
||metrics.example.com^
||cdn.example.com^$third-party
||example.com/banner/*
@@||good.example.com^
shop.example.com##.advert
news.example.com#@#.promo
"""
PLAIN = "stats.example.org\nnot a name at all\nprinter.lan\n192.0.2.7\nlocalhost\nxn--bcher-kva.example.com\n-bad-.example.com\n"


def test_reads_the_three_usual_list_shapes():
    assert parse_list(HOSTS) == {"ads.example.net", "tracker.example.org", "pixel.example.org", "beacon.example.net"}
    assert parse_list(PLAIN) == {"stats.example.org", "xn--bcher-kva.example.com"}


def test_a_rule_about_part_of_a_site_never_blocks_the_site():
    # Only the plain "block this name" rule counts. A rule with options, a path, an exception, or one that
    # hides part of a page on shop.example.com must not put that site on the list.
    assert parse_list(RULES) == {"metrics.example.com"}


def test_own_names_say_why_one_was_left_out():
    good, bad = own_names("Ads.Example.com, *.tracker.example.org\nprinter.lan  10.0.0.1 nas")
    assert good == ["ads.example.com", "tracker.example.org"]
    assert [(b["name"], b["why"].split()[0]) for b in bad] == [("printer.lan", "names"), ("10.0.0.1", "an"), ("nas", "a")]


def test_never_block_covers_the_name_and_everything_under_it():
    allow = {"example.com"}
    assert allowed("example.com", allow) and allowed("a.b.example.com", allow)
    assert not allowed("notexample.com", allow) and not allowed("example.com.evil.example.org", allow)


def _plugin(tmp_path=None, fetch=None, mode="live", **settings):
    own = SimpleNamespace(**{"lists": "", "block": "", "allow": "", "refresh_hours": 24, "max_names": 300000, **settings})
    ctx = SimpleNamespace(mode=mode, data_dir=str(tmp_path) if tmp_path else "", router_configured=True, counts_blocked=True,
                          graph=lambda: {"nodes": []}, router_only=lambda request, token: "router")
    return AdBlocker(own, ctx, fetch=fetch or (lambda url: ""))


def test_lists_are_joined_and_never_block_wins(tmp_path):
    pages = {"https://a.example/hosts": HOSTS, "https://b.example/list.txt": "ads.example.net\nshop.example.com\ncdn.shop.example.com\n"}
    p = _plugin(tmp_path, fetch=pages.__getitem__, lists="\n".join(pages), block="mine.example.org shop.example.com",
                allow="shop.example.com")
    assert p.feed() is None                                     # nothing fetched yet: nothing to hand the router
    assert p.refresh(now=p.started + 1) is True
    text, digest = p.feed(now=p.started + 1)
    assert text.split() == ["ads.example.net", "beacon.example.net", "mine.example.org", "pixel.example.org", "tracker.example.org"]
    snap = p.snapshot(now=p.started + 1)
    assert snap["count"] == 5 and snap["removedByAllow"] == 3 and snap["hash"] == digest and len(digest) == 16
    assert [x["names"] for x in snap["lists"]] == [4, 3] and not any(x["error"] for x in snap["lists"])


def test_a_failed_fetch_keeps_the_last_good_copy_even_across_a_restart(tmp_path):
    pages = {"https://a.example/hosts": HOSTS}
    p = _plugin(tmp_path, fetch=pages.__getitem__, lists="https://a.example/hosts")
    p.refresh(now=p.started + 1)
    good = p.feed(now=p.started + 1)

    def down(url):
        raise OSError("the list's server answered 503")
    again = _plugin(tmp_path, fetch=down, lists="https://a.example/hosts")           # a restart, with the publisher down
    assert again.feed() == good and again.snapshot()["lists"][0]["fromCache"] is True
    again.refresh(now=again.started + 90000)                                         # a day later it is due, and fails
    row = again.snapshot(now=again.started + 90000)["lists"][0]
    assert again.feed(now=again.started + 90000) == good and row["stale"] and "503" in row["error"]


def test_the_feed_is_held_back_rather_than_published_short(tmp_path):
    def fetch(url):
        if "slow" in url:
            raise OSError("could not be fetched (ConnectTimeout)")
        return HOSTS
    p = _plugin(None, fetch=fetch, lists="https://a.example/hosts https://slow.example/list")
    p.refresh(now=p.started + 1)
    # One list has no good copy yet. Early in a run the router is told to keep what it has ...
    assert p.feed(now=p.started + 60) is None and p.snapshot(now=p.started + 60)["waitingFor"] == ["slow.example/list"]
    # ... but a list that never arrives does not hold the others back for ever.
    assert p.feed(now=p.started + HOLD_SECONDS + 1)[0].split()[0] == "ads.example.net"
    findings = _findings(p, now=p.started + HOLD_SECONDS + 1)
    assert findings["adblock-list:slow.example/list"]["severity"] == "warning"
    assert "never been fetched" in findings["adblock-list:slow.example/list"]["title"]


def test_no_more_names_than_the_router_was_sized_for():
    page = "".join(f"n{i:04d}.example.com\n" for i in range(50))
    p = _plugin(None, fetch=lambda url: page, lists="https://a.example/big", block="mine.example.org", max_names=10)
    p.refresh(now=p.started + 1)
    names = p.feed(now=p.started + 1)[0].split()
    assert len(names) == 10 and "mine.example.org" in names                          # your own names are never the ones cut
    assert _findings(p, now=p.started + 1)["adblock-full"]["title"] == "The block list is full: 41 names left out"


class _Collect:
    def __init__(self):
        self.found = {}

    def add(self, category, key, title, **kw):
        self.found[key] = {"category": category, "title": title, **kw}


def _findings(plugin, now):
    d = _Collect()
    plugin.diagnose(d, now=now)
    return d.found


def test_findings_say_when_the_router_is_not_pulling():
    p = _plugin(None, fetch=lambda url: HOSTS, lists="https://a.example/hosts https://ftp.example/x ftp://nope")
    p.refresh(now=p.started + 1)
    assert _findings(p, now=p.started + 600).keys() == {"adblock-entry:ftp://nope"}   # too early to say the router is late
    late = _findings(p, now=p.started + 3 * 3600)
    assert late["adblock-router"]["title"] == "The router has never pulled the ad blocker's list"
    p.pulled("192.0.2.1", p.feed(now=p.started + 3 * 3600)[1], now=p.started + 3 * 3600)
    assert "adblock-router" not in _findings(p, now=p.started + 4 * 3600)
    assert "last pulled" in _findings(p, now=p.started + 40 * 3600)["adblock-router"]["title"]
    p.ctx.router_configured = False
    assert "not set up" in _findings(p, now=p.started + 4 * 3600)["adblock-router"]["title"]
    assert _findings(_plugin(None), now=0)["adblock-empty"]["severity"] == "info"


def test_sample_mode_fetches_nothing_and_shows_a_whole_picture():
    def fetch(url):
        raise AssertionError("sample mode must not reach the internet")
    p = _plugin(None, fetch=fetch, mode="sample", block="mine.example.org")
    p.refresh()
    snap = p.snapshot()
    assert snap["ready"] and snap["count"] == 10 and len(snap["lists"]) == 2
    assert snap["router"]["current"] is True and snap["router"]["lastPullS"] >= 1199
    assert _findings(p, now=p.started + 86400 * 3) == {}                              # nothing to nag about in a demo


def _client(monkeypatch, **env):
    from fastapi.testclient import TestClient
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import netgalaxy.main as main
    importlib.reload(main)
    return main, TestClient(main.app)


def test_the_plugin_is_off_unless_turned_on(settings, monkeypatch):
    monkeypatch.delenv("NETGALAXY_ADBLOCK_ENABLED", raising=False)
    main, client = _client(monkeypatch, NETGALAXY_BLOCKLIST_TOKEN="tok123")
    with client:
        assert client.get("/api/v1/plugins").json()["plugins"] == [{"id": "adblock", "title": "Ad blocker", "enabled": False,
                                                                    "tab": None, "summary": AdBlocker.summary}]
        assert client.get("/api/v1/plugins/adblock").status_code == 404
        assert client.get("/api/v1/plugins/adblock/feed?token=tok123").status_code == 404
        assert client.get("/api/v1/meta").json()["plugins"][0]["enabled"] is False
        # It is offered on the Settings page all the same: a switch and its settings under one heading.
        mine = [f for f in client.get("/api/v1/module").json()["settings"] if f["group"] == "Plugin: Ad blocker"]
        assert [f["name"] for f in mine][:2] == ["adblock_enabled", "adblock_lists"] and mine[0]["default"] is False


def test_the_feed_is_for_the_router_only(settings, state, monkeypatch):
    main, client = _client(monkeypatch, NETGALAXY_ADBLOCK_ENABLED="true", NETGALAXY_BLOCKLIST_TOKEN="tok123",
                           NETGALAXY_ADBLOCK_ALLOW="example.org")
    with client:
        assert client.get("/api/v1/plugins/adblock/feed?token=tok123").status_code == 403       # not the router's address
        main.settings.blocklist_senders = ["testclient"]
        assert client.get("/api/v1/plugins/adblock/feed?token=wrong").status_code == 403
        assert client.get("/api/v1/plugins/adblock/feed").status_code == 403
        feed = client.get("/api/v1/plugins/adblock/feed?token=tok123")
        assert feed.status_code == 200 and feed.headers["content-type"].startswith("text/plain")
        names = feed.text.split()
        assert "ads.example.net" in names and not any(n.endswith("example.org") for n in names)
        page = client.get("/api/v1/plugins/adblock").json()
        assert page["count"] == len(names) and page["hash"] == feed.headers["X-Feed-Hash"]
        assert page["router"]["from"] == "testclient" and page["router"]["current"] is True and page["router"]["lastPullS"] < 5
        assert "tok123" not in str(page)                                                          # the page never shows the token
        meta = client.get("/api/v1/meta").json()
        assert meta["plugins"][0]["tab"]["title"] == "Ad blocker"


def test_without_a_router_token_nothing_is_served(settings, monkeypatch):
    monkeypatch.delenv("NETGALAXY_BLOCKLIST_TOKEN", raising=False)
    _main, client = _client(monkeypatch, NETGALAXY_ADBLOCK_ENABLED="true")
    with client:
        assert client.get("/api/v1/plugins/adblock/feed").status_code == 404
        assert client.get("/api/v1/plugins/adblock").json()["ready"] is True                      # the page still works


def test_blocked_lookups_come_from_the_map(settings, state):
    from conftest import settle
    plugins = PLUGINS.__class__(PLUGINS.classes, "NETGALAXY").load(SimpleNamespace(
        mode="sample", data_dir="", router_configured=True, counts_blocked=True, graph=lambda: state.current(),
        router_only=lambda request, token: "router"), environ={"NETGALAXY_ADBLOCK_ENABLED": "1"})
    state.plugins = plugins
    graph = settle(state)
    blocked = plugins.get("adblock").snapshot()["blocked"]
    assert blocked["counted"] and blocked["total"] == 70 and blocked["devices"][0]["ip"] == "10.20.0.31"
    assert graph["sources"]["plugins"]["adblock"]["names"] == 9
