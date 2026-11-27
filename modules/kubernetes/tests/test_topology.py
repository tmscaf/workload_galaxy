# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

from galaxy.demo import DemoSource
from galaxy.topology import Builder, filter_graph, public


def build_demo():
    src = DemoSource()
    return public(Builder(src.snapshot(), reports=src.reports(), mesh=src.mesh(), ext_dns=src.dns()).build())


def by_id(graph):
    return {n["id"]: n for n in graph["nodes"]}


def edge(graph, s, t):
    return next((e for e in graph["edges"] if e["source"] == s and e["target"] == t), None)


def test_services_fold_into_workload_edges_with_merged_evidence():
    g = build_demo()
    e = edge(g, "wl:payments/Deployment/payments-api", "wl:payments/Deployment/fraud-check")
    assert e and set(e["evidence"]) >= {"config", "mesh", "observed"}
    assert e["via"] == ["fraud-check"]
    assert e["latency"]["p95Ms"] > 300 and e["risk"]["level"] == "critical"


def test_root_cause_vs_impact():
    g = build_demo()
    n = by_id(g)
    api = n["wl:storefront/Deployment/api-gateway"]
    # api-gateway's own trouble is only the HPA; its latency comes from payments -> fraud-check.
    assert api["risk"]["impactLevel"] == "critical"
    assert api["risk"]["impactFrom"] in ("wl:payments/Deployment/fraud-check",
                                         "wl:payments/Deployment/payments-api->wl:payments/Deployment/fraud-check")
    fraud = n["wl:payments/Deployment/fraud-check"]
    assert any("CPU" in r["text"] for r in fraud["risk"]["reasons"])


def test_problem_signals_surface_as_hotspots():
    g = build_demo()
    hot = {h["id"]: h for h in g["hotspots"]}
    assert "wl:storefront/Deployment/search" in hot                      # CrashLoopBackOff
    assert "svc:storefront/legacy-pricing" in hot                        # service with no pods
    db = by_id(g)["wl:data/StatefulSet/orders-db"]
    assert any("accept queue" in r["text"] for r in db["risk"]["reasons"])
    stripe = edge(g, "wl:payments/Deployment/payments-api", "ext:api.stripe.com")
    assert stripe and "observed" in stripe["evidence"] and "config" in stripe["evidence"]  # IP merged via DNS


def test_ingress_controller_folding():
    g = build_demo()
    router = "wl:openshift-ingress/Deployment/router-default"
    assert edge(g, "clients", router)["type"] == "exposes"
    assert edge(g, router, "entry:storefront/Route/shop")["type"] == "route"
    assert edge(g, "entry:storefront/Route/shop", "wl:storefront/Deployment/web-frontend")
    assert edge(g, "clients", "entry:storefront/Route/shop") is None
    assert edge(g, router, "wl:storefront/Deployment/web-frontend") is None


def test_filter_keeps_cross_namespace_neighbours_as_ghosts():
    g = build_demo()
    f = filter_graph(g, ["payments"], include_platform=False)
    nodes = by_id(f)
    assert nodes["wl:payments/Deployment/payments-api"]["ghost"] is False
    assert nodes["wl:storefront/Deployment/api-gateway"]["ghost"] is True     # upstream caller
    assert nodes["wl:data/StatefulSet/orders-db"]["ghost"] is True            # downstream dependency
    assert "wl:identity/Deployment/auth" not in nodes
    assert all(n["kind"] != "host" or n["name"].startswith("ocp-worker") for n in f["nodes"])


def test_templates_with_env_values_are_not_served():
    g = build_demo()
    assert all("template" not in n for n in g["nodes"])


def test_kubelet_probe_traffic_from_node_is_dropped():
    src = DemoSource()
    snap = src.snapshot()
    reports = src.reports()
    pod_ip = src.pod_ips["storefront/cart"][0]
    uid = pod_ip[2]
    for r in reports:
        for ns in r["netns"]:
            if ns["podUid"] == uid:
                ns["sockets"].append({"lip": pod_ip[1], "lport": 8080, "rip": "10.0.1.11", "rport": 50000,
                                      "state": "ESTAB", "dir": "in"})
    g = Builder(snap, reports=reports).build()
    assert not any(e["source"].startswith("ext:node") or e["target"] == "wl:storefront/Deployment/cart"
                   and e["source"] == "clients" for e in g["edges"])


def test_delayed_ack_inflated_rtt_is_not_a_network_fault():
    """Seen on the real k3s node: a watch stream to an on-node service reads srtt 10 ms, minrtt 0.018 ms."""
    src = DemoSource()
    snap = src.snapshot()
    reports = src.reports()
    cart = {uid: ip for _, ip, uid in src.pod_ips["storefront/cart"]}
    for r in reports:
        for ns in r["netns"]:
            if ns["podUid"] in cart:
                ns["sockets"] = [{"lip": cart[ns["podUid"]], "lport": 41000, "rip": src.svc_ip["storefront/catalog"],
                                  "rport": 8080, "state": "ESTAB", "dir": "out", "rtt": 10.23, "minrtt": 0.018}]
    g = Builder(snap, reports=reports).build()
    e = edge(g, "wl:storefront/Deployment/cart", "wl:storefront/Deployment/catalog")
    assert e["latency"]["rttMinMs"] == 0.018 and e["latency"]["rttP95Ms"] == 10.23
    assert not any(r["category"] == "network" for r in e["risk"]["reasons"])


def test_internet_distance_is_not_a_fault_but_a_slow_cluster_path_is():
    g = build_demo()
    stripe = edge(g, "wl:payments/Deployment/payments-api", "ext:api.stripe.com")
    assert stripe["latency"]["rttMinMs"] > 100
    assert not any("path RTT" in r["text"] for r in stripe["risk"]["reasons"])


def test_single_node_cluster_never_blames_its_node_for_relative_slowness():
    """Seen on the real single-node k3s cluster: delayed-ACK noise read as 'slower than other nodes'."""
    from galaxy.risk import score_host
    host = {"kind": "host", "name": "node-1", "ready": True, "pressure": [], "taints": [],
            "net": {"connections": 29, "retransConnections": 0, "slowConnections": 4, "retransShare": 0.0,
                    "slowShare": 1.0, "reportingNodes": 1, "rttP95Ms": 12.8}}
    assert score_host(host)["level"] == "ok"


def _with_conntrack(entries):
    src = DemoSource()
    snap = src.snapshot()
    reports = src.reports()
    reports[0]["conntrack"] = entries
    return Builder(snap, reports=reports).build(), src


def test_stale_conntrack_flows_disappear_and_udp_silence_is_not_a_failure():
    """Seen on the real cluster: ghosts of finished flows, and Steam's unanswered UDP flagged as failing."""
    vip = DemoSource().pod_ips["storefront/search"][0][1]
    g, _ = _with_conntrack([
        {"proto": "tcp", "state": "ESTABLISHED", "src": vip, "dst": "203.0.113.77", "dport": 443, "idleS": 86000},
        {"proto": "udp", "state": "", "src": vip, "dst": "203.0.113.88", "dport": 27033, "unreplied": True, "idleS": 5},
    ])
    ids = {n["id"] for n in g["nodes"]}
    assert "ext:203.0.113.77" not in ids                      # dead for a day: gone
    e = edge(g, "wl:storefront/Deployment/search", "ext:203.0.113.88")
    assert e and not e["health"].get("pending") and e["risk"]["level"] == "ok"


def test_open_but_idle_connection_is_marked_idle():
    src = DemoSource()
    snap = src.snapshot()
    reports = src.reports()
    for r in reports:
        for ns in r["netns"]:
            for s in ns["sockets"]:
                s["idleMs"] = 25 * 60 * 1000          # every socket quiet for 25 minutes
    g = Builder(snap, reports=reports).build()
    e = edge(g, "wl:identity/Deployment/auth", "wl:identity/StatefulSet/session-cache")
    assert e["traffic"]["idle"] and e["traffic"]["lastActiveS"] == 1500 and e["traffic"]["level"] == 0


def test_external_dependencies_are_classified_by_where_they_live():
    """Your network (private ranges, internal names) vs the internet vs the cluster's own infrastructure."""
    src = DemoSource()
    snap = src.snapshot()
    reports = src.reports()
    g = Builder(snap, reports=reports, ext_dns=src.dns()).build()
    scope = {n["name"]: n["scope"] for n in g["nodes"] if n["kind"] == "external"}
    assert scope["api.stripe.com"] == "internet" and scope["sftp.acme.internal"] == "lan"
    # an observed LAN device (by IP) and a public one
    host_pod = src.pod_ips["storefront/search"][0]
    reports[0]["conntrack"] = [
        {"proto": "udp", "state": "", "src": host_pod[1], "dst": "192.168.1.116", "dport": 2456, "idleS": 3},
        {"proto": "udp", "state": "", "src": host_pod[1], "dst": "198.51.100.57", "dport": 27019, "idleS": 3}]
    g = Builder(snap, reports=reports, ext_dns=src.dns()).build()
    scope = {n["name"]: n["scope"] for n in g["nodes"] if n["kind"] == "external"}
    assert scope["192.168.1.116"] == "lan" and scope["198.51.100.57"] == "internet"
    # a company's own public block can be declared internal
    g = Builder(snap, reports=reports, ext_dns=src.dns(), internal_cidrs=["198.51.0.0/16"]).build()
    assert {n["name"]: n["scope"] for n in g["nodes"] if n["kind"] == "external"}["198.51.100.57"] == "lan"


def test_inbound_users_are_split_by_where_they_come_from():
    """Users whose source address the agent sees as private are your network's users, not the internet's."""
    g = build_demo()
    n = by_id(g)
    assert n["clients"]["scope"] == "internet" and n["clients:lan"]["scope"] == "lan"
    assert all(ip.startswith("10.20.12.") for ip in n["clients:lan"]["seenFrom"])
    router = "wl:openshift-ingress/Deployment/router-default"
    assert edge(g, "clients:lan", router)["type"] == "exposes"
    assert "declared" in edge(g, "clients", router)["evidence"]          # exposure stays with End users
    # a cascade reaching either kind of user is user-facing, and users never count as its blast radius
    assert all(not c["impacted"] or not any(x.startswith("clients") for x in c["impacted"]) for c in g["cascades"])
