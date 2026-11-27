# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Sizing advice: a resource-constraint finding must come with concrete, checked ways to fix it."""
import time

from galaxy.demo import DemoSource
from galaxy.scenarios import PRESETS, SCENARIOS
from galaxy.sizing import MI, GI, Sizer, UsageHistory, nice_cpu, nice_mem, q_cpu, q_mem
from galaxy.topology import Builder, public


def run(*scenarios, history=None, min_history=0.0, mutate=None):
    src = DemoSource()
    src.set_scenarios(list(scenarios))
    snap = src.snapshot()
    if mutate:
        mutate(snap)
    return public(Builder(snap, reports=src.reports(), mesh=src.mesh(), ext_dns=src.dns(),
                          sources={"agent": src.agent_status()}, usage_history=history,
                          sizing_min_history=min_history).build())


def finding(g, subject, text):
    hits = [f for f in g["findings"] if f["subject"] == subject and text in f["title"]]
    assert hits, f"no finding '{text}' on {subject}: {[f['title'] for f in g['findings'] if f['subject'] == subject]}"
    return hits[0]


def titles(f):
    return " | ".join(o["title"] for o in f.get("options") or [])


def test_quantities_round_to_values_a_person_would_type():
    assert q_mem(nice_mem(1004 * MI * 1.3)) == "1.5Gi" and q_mem(nice_mem(300 * MI)) == "320Mi"
    assert q_mem(1280 * MI, exact=True) == "1.25Gi" and q_mem(1300 * MI, exact=True) == "1300Mi"
    assert q_cpu(nice_cpu(0.477 * 1.4)) == "700m" and q_cpu(nice_cpu(1.3)) == "1.5" and q_cpu(2.0) == "2"
    assert q_cpu(1.333, exact=True) == "1333m"


def test_options_are_well_formed_everywhere():
    g = run(*SCENARIOS)
    with_options = [f for f in g["findings"] if f.get("options")]
    assert len(with_options) >= 8
    for f in with_options:
        assert 1 <= len(f["options"]) <= 5, f["id"]
        for o in f["options"]:
            assert o["title"] and isinstance(o["admin"], bool) and set(o) == {"title", "detail", "command", "admin", "note"}
            assert "{" not in o["title"] and "None" not in o["title"] + o["detail"], o
            # Nothing here may change the cluster by itself, and no option reads a Secret.
            assert "secret" not in o["command"].lower() and "delete" not in o["command"].lower(), o


def test_oom_kill_gets_a_bigger_limit_and_a_command():
    g = run("oom-loop")
    f = finding(g, "wl:storefront/Deployment/recommendations", "OOMKilled")
    first = f["options"][0]
    assert "memory limit from 1Gi to 1.5Gi" in first["title"]
    assert first["command"] == ("oc -n storefront set resources deploy/recommendations -c recommendations "
                                "--requests=memory=1Gi --limits=memory=1.5Gi")
    assert "leak" in titles(f)
    # One story, told once: no separate "memory close to its limit" entry beside the OOM kill.
    assert not [x for x in g["findings"] if x["subject"] == f["subject"] and "Memory at" in x["title"]]


def test_exhausted_quota_offers_a_raise_a_reclaim_and_says_who_can_do_it():
    g = run("quota-exceeded")
    f = finding(g, "wl:payments/Deployment/payments-api", "quota")
    raise_, reclaim = f["options"][0], f["options"][1]
    assert raise_["admin"] and "patch resourcequota compute" in raise_["command"] and '"limits.memory":"10.5Gi"' in raise_["command"]
    assert "settlement-batch" in reclaim["detail"] and "enough to cover" in reclaim["detail"] and not reclaim["admin"]
    assert "set resources deploy/settlement-batch" in reclaim["command"] and "--limits=memory=" in reclaim["command"]
    assert "256Mi left of 8Gi" in f["detail"]


def test_scaling_out_is_checked_against_the_quota():
    g = run("quota-exceeded")
    f = finding(g, "wl:payments/Deployment/fraud-check", "CPU at")
    assert "CPU limit from 500m to 1" in f["options"][0]["title"]
    scale = next(o for o in f["options"] if "replicas" in o["title"])
    assert "Quota compute has only" in scale["note"]


def test_unschedulable_pod_is_sized_from_what_its_siblings_use():
    g = run("unschedulable")
    f = finding(g, "wl:data/Deployment/order-worker", "can't be scheduled")
    assert "asks for 14 CPU" in f["detail"] and "largest node" in f["detail"]
    lower = f["options"][0]
    assert "cpu request from 14 to" in lower["title"] and "--requests=cpu=" in lower["command"]
    assert f["options"][-1]["admin"] and "node" in f["options"][-1]["title"].lower()
    # A pod blocked by a node selector is not a sizing problem: no invented advice.
    other = finding(g, "wl:storefront/Deployment/catalog", "can't be scheduled")
    assert not other.get("options")


def test_job_that_fits_nowhere_points_at_idle_reservations():
    g = run("memory-reservations")
    f = finding(g, "wl:data/Job/search-reindex", "can't be scheduled")
    assert "analytics-warehouse" in " ".join(o["detail"] for o in f["options"])
    assert all("set resources job/" not in o["command"] for o in f["options"])      # a Job can't be resized in place
    assert "can't be resized in place" in f["optionsNote"]
    node = next(x for x in g["findings"] if x["category"] == "node" and "requests reserve" in x["title"])
    assert "Take back" in node["options"][0]["title"]


def test_autoscaler_at_its_ceiling_gets_the_replica_count_the_load_calls_for():
    g = run("hpa-maxed")
    f = finding(g, "wl:storefront/Deployment/api-gateway", "Autoscaler pinned")
    assert f["severity"] == "warning" and "118%" in f["detail"]
    assert f["options"][0]["title"] == "Raise maxReplicas from 3 to 7"
    assert f["options"][0]["command"].endswith("""patch hpa api-gateway --type merge -p '{"spec":{"maxReplicas":7}}'""")
    assert len([x for x in g["findings"] if x["subject"] == f["subject"] and "utoscaler" in x["title"]]) == 1


def test_node_out_of_memory_names_the_pod_that_took_it():
    g = run("memory-pressure")
    nodes = [f for f in g["findings"] if f["category"] == "node" and f["subject"] == "host:ocp-worker-2" and f.get("options")]
    assert len(nodes) == 1                                  # pressure and "95% used" are one story
    assert "etl-backfill" in nodes[0]["options"][0]["title"] and "--limits=memory=" in nodes[0]["options"][0]["command"]


def test_oversized_is_only_said_after_watching_long_enough():
    assert not [f for f in run("oversized", min_history=3600)["findings"] if f["category"] == "sizing"]
    g = run("oversized", min_history=0)
    f = finding(g, "wl:data/Deployment/report-builder", "Reserves far more")
    assert f["severity"] == "info" and "--requests=cpu=" in f["options"][0]["command"] and "memory=1.5Gi" in f["options"][0]["command"]
    real = lambda graph: sorted(x["id"] for x in graph["findings"] if x["severity"] != "info")
    assert real(g) == real(run(min_history=0))              # information, not a problem


def test_history_keeps_the_peak_not_the_last_reading():
    h = UsageHistory()
    now = time.time()
    h.observe(now - 7300, {("w", "c"): (0.9, 800 * MI)})
    h.observe(now, {("w", "c"): (0.2, 300 * MI)})
    cpu, mem, span = h.peak(("w", "c"), now)
    assert cpu == 0.9 and mem == 800 * MI and 7200 < span < 7400
    for i in range(40):                                     # a day of buckets at most
        h.observe(now + i * 7200, {("w", "c"): (0.1, 1)})
    assert len(h.series[("w", "c")]) == UsageHistory.KEEP


def test_peak_from_earlier_refreshes_drives_the_recommendation():
    h = UsageHistory()
    h.first[("wl:storefront/Deployment/recommendations", "recommendations")] = time.time() - 9000
    h.series[("wl:storefront/Deployment/recommendations", "recommendations")] = [[0, 0.5, 1400 * MI]]
    f = finding(run("oom-loop", history=h), "wl:storefront/Deployment/recommendations", "OOMKilled")
    assert "to 2Gi" in f["options"][0]["title"] and "over the last 2h" in f["options"][0]["detail"]
    assert "single reading" not in f.get("optionsNote", "")


def test_a_limitrange_cap_is_called_out():
    def cap(snap):
        snap["limitranges"] = [{"metadata": {"name": "limits", "namespace": "storefront"},
                                "spec": {"limits": [{"type": "Container", "max": {"memory": "1Gi"}}]}}]
    f = finding(run("oom-loop", mutate=cap), "wl:storefront/Deployment/recommendations", "OOMKilled")
    assert "LimitRange limits caps a container at 1Gi" in f["options"][0]["note"]


def test_helm_managed_workloads_are_flagged():
    def helm(snap):
        for d in snap["deployments"]:
            if d["metadata"]["name"] == "recommendations":
                d["metadata"].setdefault("annotations", {})["meta.helm.sh/release-name"] = "storefront"
    f = finding(run("oom-loop", mutate=helm), "wl:storefront/Deployment/recommendations", "OOMKilled")
    assert "Deployed with Helm (storefront)" in f["optionsNote"]


def test_a_failing_advisor_never_breaks_the_board(monkeypatch):
    monkeypatch.setattr(Sizer, "_index", lambda self: 1 / 0)
    g = run("oom-loop")
    assert finding(g, "wl:storefront/Deployment/recommendations", "OOMKilled")["fix"]


def test_sizing_preset_names_real_scenarios():
    preset = next(p for p in PRESETS if p["id"] == "sizing")
    assert all(s in SCENARIOS for s in preset["scenarios"])
    g = run(*preset["scenarios"])
    assert sum(1 for f in g["findings"] if f.get("options")) >= 7
