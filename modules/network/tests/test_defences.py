# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""The defences on the map: DNS blocking, the firewall and your block list, and what was stopped at each."""
from conftest import settle

from netgalaxy.graphops import filter_graph


def by_name(graph, name):
    return next(n for n in graph["nodes"] if n["name"] == name)


def defences(g):
    return {n["defence"]["type"]: n for n in g["nodes"] if n.get("scope") == "defence"}


def stopped_at(g, nid):
    return {e["source"]: e for e in g["edges"] if e["target"] == nid}


def test_dns_blocking_shows_who_is_being_blocked(state):
    g = settle(state)
    dns = defences(g)["dns"]
    assert dns["kind"] == "external" and dns["name"] == "DNS blocking" and dns["risk"]["level"] == "ok"
    assert dns["defence"]["total"] == 70 and dns["defence"]["devices"] == 1 and dns["defence"]["summary"] == "70 blocked now"
    lines = stopped_at(g, "def:dns")
    phone = next(n for n in g["nodes"] if n.get("device", {}).get("ip") == "10.20.0.31")
    assert list(lines) == [phone["id"]]
    e = lines[phone["id"]]
    # Something stopped, not traffic: a count, and no rate.
    assert e["blocked"] is True and e["stopped"] == 70 and e["type"] == "stopped" and e["traffic"]["bytesPerSec"] == 0
    assert "70 connections to the block page" in e["details"][0]
    # ... and so it is not listed among the connections.
    assert not any(r["to"] == "DNS blocking" for r in state.index().call_rows())
    # The block page itself is still not drawn as a device or a service.
    assert not any("10.99.0.1" in (n.get("addresses") or []) for n in g["nodes"])


def test_the_firewall_shows_what_was_turned_away(state):
    g = settle(state)
    fw = defences(g)["firewall"]["defence"]
    assert fw["blockedIn"] > 0 and fw["sources"] > 0 and fw["windowMinutes"] == 60 and fw["topSources"]
    door = stopped_at(g, "def:firewall")["clients"]                 # the internet reaching in, stopped at the door
    assert door["stopped"] == fw["blockedIn"] and "turned away" in door["details"][0]
    assert fw["summary"] == f"{fw['blockedIn']} turned away"


def test_your_block_list_appears_once_you_block_something(state):
    assert "blocks" not in defences(settle(state))
    state.blocks.add("203.0.113.66", reason="known bad", by="admin")
    g = settle(state)
    blocks = defences(g)["blocks"]
    assert blocks["defence"]["entries"] == 1 and blocks["defence"]["summary"] == "1 address blocked"
    bad = by_name(g, "203.0.113.66")
    assert blocks["defence"]["services"] == [{"id": bad["id"], "name": "203.0.113.66"}]
    assert stopped_at(g, "def:blocks")[bad["id"]]["blocked"] is True   # the blocked service hangs off the list


def test_no_data_no_defence(state):
    state.settings.sinkhole_ips = []
    state.events.events.clear()
    from netgalaxy.graph import Builder
    g = Builder(state.settings, state.inventory, state.sample.snapshot(0), events={}).build()
    assert defences(g) == {}


def test_defences_stay_on_the_map_when_it_is_scoped_to_some_groups(state):
    g = settle(state)
    scoped = filter_graph(g, ["printers"], True)
    kept = defences(scoped)
    assert set(kept) == {"dns", "firewall"}                          # landmarks, whatever groups are shown
    # ... with only what was stopped for the devices in view: the phone is not, so its line is not.
    assert stopped_at(scoped, "def:dns") == {}
    phones = filter_graph(g, ["phones-tablets"], True)
    assert len(stopped_at(phones, "def:dns")) == 1


def test_the_ad_blocker_adds_its_numbers_to_dns_blocking(state):
    from types import SimpleNamespace

    from netgalaxy.plugins import PLUGINS
    state.plugins = PLUGINS.__class__(PLUGINS.classes, "NETGALAXY").load(SimpleNamespace(
        mode="sample", data_dir="", router_configured=True, counts_blocked=True, graph=lambda: state.current(),
        router_only=lambda request, token: "router"), environ={"NETGALAXY_ADBLOCK_ENABLED": "1"})
    assert defences(settle(state))["dns"]["defence"]["adblock"] == {"names": 9, "ready": True}
    state.plugins = None
    assert defences(settle(state))["dns"]["defence"]["adblock"] is None
