# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Large estates: the page gets a picture one screen can show, and small ones are left alone."""
import json

from galaxy.demo import DemoSource
from galaxy.scale import Index, collapse
from galaxy.topology import Builder, filter_graph, public


def build(scale=""):
    src = DemoSource(scale)
    snap = src.snapshot()
    return public(Builder(snap, reports=src.reports(), mesh=src.mesh(), ext_dns=src.dns(),
                          sources={"agent": src.agent_status()}).build())


BIG = build("70x12")           # 75 projects, ~850 workloads: over both limits, quick to build


def view(expand=(), graph=BIG, **kw):
    g, cmap = collapse(filter_graph(graph, None, False), expand=list(expand), **kw)
    g.pop("_scoped", None)
    return g, cmap


def test_small_cluster_is_served_exactly_as_before():
    small = build()
    g, cmap = view(graph=small)
    assert g["view"]["level"] == "workloads" and not g["view"]["collapsed"]
    assert not any(n.get("aggregate") for n in g["nodes"])
    assert {n["id"] for n in g["nodes"]} == {n["id"] for n in filter_graph(small, None, False)["nodes"]}
    assert all(f["at"] == f["subject"] for f in g["findings"])
    assert len(g["findings"]) >= 15 and g["summary"]["problems"] >= 5      # nothing folded away at this size


def test_large_estate_rolls_up_to_business_units_within_budget():
    g, cmap = view()
    v = g["view"]
    assert v["level"] == "units" and v["collapsed"] and v["workloads"] > 600 and v["projects"] > 60
    units = [n for n in g["nodes"] if n.get("aggregate") == "unit"]
    assert 10 <= len(units) <= 25 and len(g["nodes"]) < 60
    assert sum(u["counts"]["workloads"] for u in units if not u["ghost"]) == v["workloads"]
    retail = next(u for u in units if u["name"] == "retail")
    assert retail["kind"] == "workload" and retail["expand"] == "grp:retail"          # drawn like a workload
    assert len(retail["pods"]) <= 14 and all(p["pseudo"] and p["child"].startswith("proj:") for p in retail["pods"])
    assert retail["counts"]["healthy"] + retail["counts"]["failing"] == retail["counts"]["workloads"]
    # every real node is drawn as something that is on the page, and no call is lost or doubled
    shown = {n["id"] for n in g["nodes"]}
    assert all(cmap[n["id"]] in shown for n in BIG["nodes"] if n["id"] in cmap)
    assert all(e["source"] in shown and e["target"] in shown and e["source"] != e["target"] for e in g["edges"])
    assert len({e["id"] for e in g["edges"]}) == len(g["edges"])
    assert len(json.dumps(g)) < 1_500_000


def test_units_and_projects_open_in_place_and_the_budget_closes_the_oldest():
    g, _ = view(["grp:retail"])
    kinds = [n.get("aggregate") for n in g["nodes"]]
    assert kinds.count("project") >= 3 and not any(n["id"] == "grp:retail" for n in g["nodes"])
    ns = next(n["project"] for n in g["nodes"] if n.get("aggregate") == "project")
    g, cmap = view(["grp:retail", f"ns:{ns}"])
    real = [n for n in g["nodes"] if n.get("namespace") == ns and not n.get("aggregate")]
    assert len(real) >= 3 and all(cmap[n["id"]] == n["id"] for n in real)
    assert g["view"]["expanded"] == ["grp:retail", f"ns:{ns}"]
    # too much opened at once: the oldest expansion gives way, and the response says so
    everything = [f"ns:{p['name']}" for p in BIG["namespaces"] if not p["platform"]]
    g, _ = view(everything, budget=150)
    assert g["view"]["dropped"] and len(g["nodes"]) <= 190 and g["view"]["expanded"] == everything[len(g["view"]["dropped"]):]


def test_one_bad_workload_does_not_paint_a_whole_unit_red():
    g, _ = view()
    for u in (n for n in g["nodes"] if n.get("aggregate") == "unit"):
        k = u["counts"]
        if k["failing"] and k["failing"] / k["workloads"] < 0.1 and k["down"] / k["workloads"] < 0.03:
            assert u["risk"]["level"] == "warning", u["name"]
        if not k["failing"]:
            assert u["risk"]["level"] == "ok"


def test_the_same_problem_on_many_workloads_is_reported_once():
    g, _ = view()
    crash = next(p for p in g["patterns"] if "crash-looping" in p["label"])
    assert crash["count"] >= 4 and crash["projects"] >= 2 and crash["cause"] and crash["fix"] and len(crash["examples"]) <= 5
    assert all(x["at"] for x in crash["examples"])                           # each example can be shown on the map
    members = {x["subject"] for x in crash["examples"]}
    assert not any(f["subject"] in members and f["category"] == "crash" for f in g["findings"])   # folded into the pattern
    assert g["findingsMatching"] < g["summary"]["findings"] and g["summary"]["problems"] >= crash["count"]   # totals count everything
    listed = filter_graph(BIG, None, False, pattern=crash["key"])
    assert len({f["subject"] for f in listed["findings"] if f["category"] == "crash"}) == crash["count"]
    gaps = next(p for p in g["patterns"] if p["severity"] == "info")
    assert gaps["count"] > 100                                              # hundreds of hygiene notes become one line


def test_tables_detail_and_search_work_against_the_whole_estate():
    ix = Index(BIG)
    page = ix.page(ix.workload_rows(), namespaces=None, platform=False, q="", sort="risk", desc=True, offset=0,
                   limit=20, only_problems=True)
    assert 0 < page["total"] < page["scope"] and len(page["rows"]) == min(20, page["total"])
    assert all(r["level"] in ("warning", "critical") for r in page["rows"])
    assert page["rows"][0]["risk"] >= page["rows"][-1]["risk"]
    by_name = ix.page(ix.workload_rows(), namespaces=None, platform=False, q="", sort="name", desc=False, offset=0,
                      limit=5, only_problems=False)["rows"]
    assert [r["name"] for r in by_name] == sorted(r["name"] for r in by_name)
    one = ix.page(ix.workload_rows(), namespaces=["retail-web"], platform=False, q="", sort="risk", desc=True, offset=0,
                  limit=200, only_problems=False)
    assert one["rows"] and {r["ns"] for r in one["rows"]} == {"retail-web"}
    calls = ix.page(ix.call_rows(), namespaces=None, platform=False, q="", sort="p95", desc=True, offset=0, limit=10,
                    only_problems=False)
    assert calls["rows"][0]["p95"] >= calls["rows"][-1]["p95"]
    wid = page["rows"][0]["id"]
    d = ix.detail(wid)
    assert d["nodes"][0]["id"] == wid and d["nodes"][0]["pods"] and d["findings"]
    assert all(e["source"] == wid or e["target"] == wid for e in d["edges"])
    assert ix.detail("wl:nope/Deployment/x") is None
    hit = ix.search(page["rows"][0]["name"])[0]
    assert hit["name"] == page["rows"][0]["name"]


def test_problems_only_draws_what_is_failing_with_its_neighbourhood():
    g, cmap = view(focus=True)
    v = g["view"]
    assert v["focus"] and v["collapsed"] and 0 < v["focusShown"] <= v["focusTotal"] and len(g["nodes"]) <= v["budget"]
    drawn = {n["id"]: n for n in g["nodes"]}
    failing = [n for n in drawn.values() if n["kind"] == "workload" and not n.get("aggregate") and n["risk"]["level"] != "ok"]
    assert len(failing) >= v["focusShown"] * 0.9
    worst = max((n for n in BIG["nodes"] if n["kind"] == "workload" and not n.get("platform")), key=lambda n: n["risk"]["score"])
    assert worst["id"] in drawn                                           # the worst problem is always on the map
    for e in BIG["edges"]:                                                # ...with what calls it and what it calls
        if worst["id"] in (e["source"], e["target"]):
            other = e["target"] if e["source"] == worst["id"] else e["source"]
            assert cmap.get(other) in drawn
    # everything else is still accounted for, inside its unit
    units = [n for n in g["nodes"] if n.get("aggregate") in ("unit", "project")]
    individually = sum(1 for n in drawn.values() if n["kind"] == "workload" and not n.get("aggregate") and not n.get("ghost"))
    assert sum(u["counts"]["workloads"] for u in units if not u["ghost"]) + individually == v["workloads"]
    assert all(e.get("minor") for e in g["edges"] if e["source"].startswith("grp:") and e["target"].startswith("grp:"))


def test_problems_only_works_on_a_small_cluster_too():
    small = build()
    g, _ = view(graph=small, focus=True)
    names = {n["name"] for n in g["nodes"] if n["kind"] == "workload" and not n.get("aggregate")}
    assert {"fraud-check", "search", "orders-db"} <= names                  # the failing ones, individually
    assert "payments-api" in names                                          # a caller of fraud-check
    assert any(n.get("aggregate") == "project" for n in g["nodes"])          # the healthy rest, rolled up
    assert g["view"]["focusShown"] == g["view"]["focusTotal"]
