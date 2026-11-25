"""Diagnostics: each failure must surface as a finding that names the cause and the next step."""
from galaxy.demo import DemoSource
from galaxy.scenarios import SCENARIOS
from galaxy.topology import Builder, filter_graph, public


def run(*scenarios):
    src = DemoSource()
    src.set_scenarios(list(scenarios))
    snap = src.snapshot()
    return public(Builder(snap, reports=src.reports(), mesh=src.mesh(), ext_dns=src.dns(),
                          sources={"agent": src.agent_status()}).build())


def find(g, category, subject=None, text=None):
    return [f for f in g["findings"] if f["category"] == category and (subject is None or f["subject"] == subject)
            and (text is None or text in f["title"])]


def one(g, category, subject=None, text=None):
    hits = find(g, category, subject, text)
    assert hits, f"no {category} finding for {subject} / {text}: {[f['title'] for f in g['findings'][:12]]}"
    return hits[0]


def test_every_finding_says_what_why_and_what_to_do():
    g = run(*SCENARIOS)
    assert len(g["findings"]) > 40
    for f in g["findings"]:
        assert f["title"] and f["severity"] in ("critical", "warning", "info") and f["category"], f
        assert f["cause"] and f["fix"], f["id"]
    ids = [f["id"] for f in g["findings"]]
    assert len(ids) == len(set(ids))


def test_baseline_names_root_causes_and_demotes_their_symptoms():
    g = run()
    fraud = one(g, "saturation", "wl:payments/Deployment/fraud-check", "CPU")
    assert fraud["root"] and fraud["blast"] >= 3 and fraud["userFacing"]
    slow = one(g, "latency", "wl:storefront/Deployment/api-gateway->wl:payments/Deployment/payments-api")
    assert not slow["root"] and slow["causedBy"] == "wl:payments/Deployment/fraud-check"
    crash = one(g, "crash", "wl:storefront/Deployment/search")
    assert "exit 1" in crash["title"] and any("--previous" in c for c in crash["commands"])
    assert one(g, "network", text="legacy-pricing selects no pods")["severity"] == "critical"
    # a healthy platform component must not be accused (the router really listens on 80 and 443)
    assert not find(g, "network", "wl:openshift-ingress/Deployment/router-default")
    # the pod's state is said once, not once per signal
    assert not [f for f in g["findings"] if f["subject"] == "wl:storefront/Deployment/search" and "pod(s) in" in f["title"]]


def test_stuck_rollout_shows_what_changed_and_why_it_is_blocked():
    g = run("bad-rollout")
    cart = "wl:storefront/Deployment/cart"
    n = next(x for x in g["nodes"] if x["id"] == cart)
    assert n["rollout"]["state"] == "stuck" and any("image cart: cart:1.4.0 → cart:1.6.0-rc1" in c for c in n["rollout"]["changes"])
    image = one(g, "image", cart)
    assert "does not exist" in image["title"] and "manifest unknown" in image["evidence"][0]
    rollout = one(g, "rollout", cart)
    assert rollout["effectOf"] == image["id"] and any("rollout undo" in c for c in rollout["commands"])
    assert any(c["kind"] == "rollout" and c["subject"] == cart for c in g["changes"])


def test_missing_configmap_is_named_with_the_likely_typo():
    f = one(run("missing-config"), "config", "wl:storefront/Deployment/recommendations")
    assert "recommendations-settings is missing" in f["title"] and "recommendation-settings" in f["cause"]


def test_exit_code_is_translated():
    f = one(run("crash-on-start"), "crash", "wl:identity/Deployment/user-profile")
    assert "command not found (exit 127)" in f["title"] and f["podCount"] == 2 and "start-server.sh" in f["evidence"][0]
    assert f["blast"] >= 1


def test_quota_and_security_policy_block_pod_creation():
    g = run("quota-exceeded", "scc-denied")
    q = one(g, "quota", "wl:payments/Deployment/payments-api")
    assert "quota is exhausted" in q["title"] and "limits.memory=8Gi" in q["evidence"][0]
    assert one(g, "quota", text="Project quota 97% used")["namespace"] == "payments"
    scc = one(g, "admission", "wl:data/Deployment/order-worker")
    assert "security policy" in scc["title"] and any("scc-subject-review" in c for c in scc["commands"])
    assert one(g, "rollout", "wl:data/Deployment/order-worker")["effectOf"] == scc["id"]


def test_unbound_claim_explains_the_unschedulable_pod():
    g = run("pvc-pending")
    wid = "wl:data/StatefulSet/search-index"
    pvc = one(g, "storage", wid)
    assert 'StorageClass "fast-ssd" does not exist' in pvc["cause"]
    assert one(g, "scheduling", wid)["effectOf"] == pvc["id"]
    n = next(x for x in g["nodes"] if x["id"] == wid)
    assert {"Bound", "Pending"} == {s["phase"] for s in n["storage"]}


def test_probe_failures_are_read_from_events():
    g = run("probe-failing", "liveness-kill")
    ready = one(g, "probes", "wl:identity/Deployment/auth")
    assert "readiness probe" in ready["title"] and "HTTP 404" in ready["cause"]
    assert next(x for x in g["nodes"] if x["id"] == "wl:identity/Deployment/auth")["rollout"]["state"] == "failed"
    live = one(g, "probes", "wl:storefront/Deployment/web-frontend")
    assert "liveness probe" in live["title"] and live["podCount"] == 3
    assert not find(g, "crash", "wl:storefront/Deployment/web-frontend")       # it is not an application crash


def test_service_wiring_and_network_policy():
    g = run("port-mismatch", "netpol-block")
    port = one(g, "network", "wl:storefront/Deployment/catalog", "port 8081")
    assert "listen on 8080" in port["cause"]
    victims = [f for f in g["findings"] if f["causedBy"] == "wl:storefront/Deployment/catalog"]
    assert len(victims) >= 3 and all(not f["root"] for f in victims)
    blocked = {f["subjectName"] for f in find(g, "network", text="NetworkPolicy blocks")}
    assert blocked == {"catalog → orders-db", "ledger → orders-db", "user-profile → orders-db"}   # not order-worker (same tier)


def test_route_job_and_platform_findings():
    g = run("route-broken", "failed-job", "operator-degraded")
    missing = one(g, "network", "entry:storefront/Route/checkout")
    assert "cart-svc" in missing["title"] and '"cart"' in missing["cause"]
    assert "HostAlreadyClaimed" in one(g, "network", "entry:storefront/Route/promo")["title"]
    assert "BackoffLimitExceeded" in one(g, "batch", text="nightly-export")["title"]
    assert one(g, "platform", text="operator ingress is degraded")["severity"] == "warning"
    assert g["platform"]["openshift"]["degraded"] == ["ingress"] and g["platform"]["nodes"]["cordoned"] == 1
    assert any(c["role"] == "ingress" for c in g["platform"]["components"])


def test_filtering_keeps_a_projects_findings_and_cluster_level_ones():
    g = run("quota-exceeded", "operator-degraded", "bad-rollout")
    f = filter_graph(g, ["payments"], include_platform=False)
    namespaces = {x["namespace"] for x in f["findings"]}
    assert "payments" in namespaces and "storefront" not in namespaces
    assert any(x["category"] == "platform" for x in f["findings"])           # explains symptoms in any project
    assert all(c["namespace"] in ("payments", "") for c in f["changes"])
    row = next(p for p in g["namespaces"] if p["name"] == "payments")
    assert row["quota"] > 0.9 and row["findings"]["critical"] >= 1 and row["top"]


def test_per_container_state_is_reported():
    g = run("oom-loop")
    n = next(x for x in g["nodes"] if x["id"] == "wl:storefront/Deployment/recommendations")
    last = [c["last"] for p in n["pods"] for c in p["containerInfo"] if c["last"]]
    assert last and all(x["reason"] == "OOMKilled" and x["exitCode"] == 137 for x in last)
    assert "out of memory" in one(g, "crash", n["id"])["title"]


def test_events_for_hidden_namespaces_are_ignored():
    """Seen on the real cluster: an event on this tool's own (hidden) pod crashed every refresh."""
    src = DemoSource()
    snap = src.snapshot()
    pod = next(p for p in snap["pods"] if p["metadata"]["namespace"] == "observability")
    for kind, name in (("Pod", pod["metadata"]["name"]), ("DaemonSet", "otel-collector"), ("Pod", "gone-7d9f8-abcde")):
        snap["events"].append({"type": "Warning", "reason": "Unhealthy", "message": "Readiness probe failed", "count": 3,
                               "involvedObject": {"kind": kind, "namespace": "observability", "name": name}})
    g = public(Builder(snap, reports=src.reports(), hidden_namespaces={"observability"}).build())
    assert not any(n.get("namespace") == "observability" for n in g["nodes"])
    assert not any(f["namespace"] == "observability" for f in g["findings"])
    assert not any(c["namespace"] == "observability" for c in g["changes"])
