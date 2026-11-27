# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Each fault scenario must surface its root cause on the right strata layer - and nowhere else."""
import pytest

from galaxy.demo import DemoSource
from galaxy.scenarios import SCENARIOS
from galaxy.topology import Builder, public


def run(*scenarios):
    src = DemoSource()
    src.set_scenarios(list(scenarios))
    snap = src.snapshot()
    g = public(Builder(snap, reports=src.reports(), mesh=src.mesh(), ext_dns=src.dns(),
                       sources={"agent": src.agent_status()}).build())
    return g, {n["id"]: n for n in g["nodes"]}


def bad_hosts(nodes):
    return {nid for nid, n in nodes.items() if n["kind"] == "host" and n["risk"]["level"] != "ok"}


def pods(nodes, wid):
    return nodes[wid]["pods"]


def test_baseline_hosts_are_healthy():
    _, nodes = run()
    assert bad_hosts(nodes) == set()


def test_memory_pressure_evicts_and_blames_the_node():
    _, nodes = run("memory-pressure")
    assert bad_hosts(nodes) == {"host:ocp-worker-2"}
    h = nodes["host:ocp-worker-2"]
    assert h["risk"]["reasons"][0]["text"].startswith("MemoryPressure") and h["memRatio"] > 0.9
    evicted = [p for n in nodes.values() if n["kind"] == "workload" for p in n["pods"] if p["level"] == "evicted"]
    assert len(evicted) == 2 and all(p["node"] == "ocp-worker-2" for p in evicted)
    impacted = [n for n in nodes.values() if n["kind"] == "workload" and n["risk"]["impactFrom"] == "host:ocp-worker-2"]
    assert len(impacted) >= 3


def test_memory_reservations_flag_requests_not_usage():
    _, nodes = run("memory-reservations")
    h = nodes["host:ocp-worker-1"]
    assert h["memRequestRatio"] >= 0.95 and h["memRatio"] < 0.9
    texts = " ".join(r["text"] for r in h["risk"]["reasons"])
    assert "Memory requests reserve" in texts and "overcommitted" in texts
    pseudo = nodes["host:(unscheduled)"]
    assert pseudo["pending"][0]["pod"].startswith("search-reindex") and "Insufficient memory" in pseudo["pending"][0]["message"]


def test_unschedulable_pods_land_on_the_pseudo_host_with_reasons():
    _, nodes = run("unschedulable")
    pseudo = nodes["host:(unscheduled)"]
    assert pseudo["podCount"] == 5
    msgs = " ".join(p["message"] for p in pseudo["pending"])
    assert "node affinity/selector" in msgs and "untolerated taint" in msgs and "Insufficient cpu" in msgs
    cat = nodes["wl:storefront/Deployment/catalog"]
    assert cat["replicas"]["ready"] == 3 and cat["replicas"]["desired"] == 5
    assert any("can't be scheduled" in r["text"] for r in cat["risk"]["reasons"])
    assert all(p["hostId"] == "host:(unscheduled)" for p in cat["pods"] if not p["node"])


def test_node_not_ready_takes_its_pods_down_and_silences_the_agent():
    _, nodes = run("node-not-ready")
    h = nodes["host:ocp-worker-3"]
    assert not h["ready"] and h["agentStale"] and h["risk"]["level"] == "critical"
    on_node = [p for n in nodes.values() if n["kind"] == "workload" for p in n["pods"] if p["node"] == "ocp-worker-3"]
    assert on_node and all(p["level"] == "critical" for p in on_node)


def test_noisy_neighbour_is_found_on_the_pod_and_the_node():
    _, nodes = run("noisy-neighbour")
    assert bad_hosts(nodes) == {"host:ocp-worker-1"}
    hog = pods(nodes, "wl:data/Job/nightly-export")[0]
    assert any("Noisy neighbour" in i for i in hog["issues"])


def test_packet_loss_is_localised_to_one_node():
    _, nodes = run("node-network")
    assert bad_hosts(nodes) == {"host:ocp-worker-2"}
    assert nodes["host:ocp-worker-2"]["net"]["retransShare"] >= 0.6


@pytest.mark.parametrize("sid,wid", [("oom-loop", "wl:storefront/Deployment/recommendations"),
                                     ("bad-rollout", "wl:storefront/Deployment/cart"),
                                     ("db-outage", "wl:data/StatefulSet/orders-db")])
def test_app_level_faults_leave_hosts_green(sid, wid):
    g, nodes = run(sid)
    assert bad_hosts(nodes) == set()
    assert nodes[wid]["risk"]["level"] == "critical"
    assert wid in {h["id"] for h in g["hotspots"]}


def test_db_outage_cascades_impact_upstream():
    _, nodes = run("db-outage")
    for caller in ("wl:storefront/Deployment/catalog", "wl:payments/StatefulSet/ledger"):
        assert nodes[caller]["risk"]["impactFrom"] == "wl:data/StatefulSet/orders-db"


def test_every_scenario_builds_and_scenarios_combine():
    for sid in SCENARIOS:
        run(sid)
    _, nodes = run(*SCENARIOS)
    assert {"host:ocp-worker-1", "host:ocp-worker-2", "host:ocp-worker-3", "host:(unscheduled)"} <= bad_hosts(nodes)


def test_cascades_rank_the_spreading_root_first():
    g, _ = run("node-network")
    top = g["cascades"][0]
    assert top["root"] == "host:ocp-worker-2" and top["userFacing"] and top["count"] >= 10
    assert set(top["dominant"]) <= set(top["impacted"]) and "clients" not in top["impacted"]
    g, _ = run("db-outage")
    assert g["cascades"][0]["root"] == "wl:data/StatefulSet/orders-db"
    assert "wl:storefront/Deployment/catalog" in g["cascades"][0]["impacted"]


def test_cascades_survive_namespace_filtering():
    from galaxy.topology import filter_graph
    g, _ = run("db-outage")
    f = filter_graph(g, ["payments"], include_platform=False)
    assert f["cascades"] and all(set(c["impacted"]) <= {n["id"] for n in f["nodes"]} for c in f["cascades"])


def test_filtered_cascade_main_cause_is_a_subset_of_its_blast_radius():
    from galaxy.topology import filter_graph
    g, _ = run("node-network")
    for f in (filter_graph(g, None, include_platform=False), filter_graph(g, ["payments"], include_platform=False)):
        for c in f["cascades"]:
            assert set(c["dominant"]) <= set(c["impacted"]) and c["count"] == len(c["impacted"])
