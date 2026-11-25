"""The shared core: settings, graph builders, scoping and the server-side tables."""
from galaxy_core import nodes as N
from galaxy_core.graphops import filter_graph, summarize
from galaxy_core.scale import Index, collapse
from galaxy_core.settings import Field, load, missing, public, schema

FIELDS = [Field("host", label="Host", required=True), Field("password", secret=True, required=True),
          Field("port", kind="int", default=443, minimum=1), Field("verify", kind="bool", default=False),
          Field("tags", kind="list"), Field("mode", default="live", choices=("live", "sample"))]


def test_settings_load_from_environment_and_saved_values():
    env = {"X_HOST": "esx.example", "X_PORT": "8443", "X_VERIFY": "yes", "X_TAGS": "a, b,,c", "X_MODE": "nonsense"}
    s = load(FIELDS, "X", environ=env)
    assert (s.host, s.port, s.verify, s.tags, s.mode) == ("esx.example", 8443, True, ["a", "b", "c"], "live")
    # saved values win over the environment; a bad number falls back to the default; a minimum is enforced
    s = load(FIELDS, "X", {"host": "other", "port": "abc"}, environ=env)
    assert (s.host, s.port) == ("other", 443)
    assert load(FIELDS, "X", {"port": -5}, environ={}).port == 1


def test_settings_never_expose_a_secret():
    s = load(FIELDS, "X", {"host": "h", "password": "hunter2"}, environ={})
    assert missing(FIELDS, s) == []
    assert missing(FIELDS, load(FIELDS, "X", environ={})) == ["Host", "password"]
    assert public(FIELDS, s)["password"] is True and "hunter2" not in str(public(FIELDS, s))
    sch = {f["name"]: f for f in schema(FIELDS, "X")}
    assert sch["password"]["secret"] and sch["password"]["default"] is None and sch["host"]["env"] == "X_HOST"


def _graph():
    nodes = {}
    edges = {}
    nodes["host:h1"] = N.host("host:h1", "h1", "Hypervisor", cpu_capacity=8, mem_capacity=64e9, cpu_used=2, mem_used=32e9)
    nodes["host:spare"] = N.host("host:spare", "spare", "Hypervisor")
    nodes["host:pinned"] = N.host("host:pinned", "pinned", "Hypervisor", pinned=True)
    for name, group, on in (("a", "red", True), ("b", "red", True), ("c", "blue", False)):
        nid = f"wl:{group}/VM/{name}"
        nodes[nid] = N.workload(nid, name, group, "Virtual machine", pods=[N.pod("slot", "host:h1")] if on else [], running=on,
                                thing={"ip": f"10.0.0.{len(nodes)}"})
    nodes["ext:net"] = N.external("ext:net", "net", "Port group", scope="lan")
    nodes["ext:net2"] = N.external("ext:net2", "net2", "Port group", scope="lan")
    nodes["ext:nic"] = N.external("ext:nic", "nic0", "Uplink", scope="uplink")
    for src, dst, kind, rate in (("wl:red/VM/a", "ext:net", "attached", 5000.0), ("wl:blue/VM/c", "ext:net", "attached", 0.0),
                                 ("ext:net", "ext:nic", "uplink", 5000.0), ("ext:net2", "ext:nic", "uplink", 0.0),
                                 ("wl:red/VM/b", "ext:net2", "attached", 10.0)):
        e = N.edge(src, dst, kind, bytes_per_sec=rate, observed=rate > 0)
        edges[e["id"]] = e
    f = N.Findings(nodes, edges, {"cap:full": "things that are full"})
    f.add("cap", "full:a", "a is full", subject="wl:red/VM/a", weight=0.7, cause="because", fix="empty it",
          options=[{"title": "Empty it"}], options_note="measured once")
    f.add("cap", "note:b", "b is worth knowing", subject="wl:red/VM/b", severity="info", weight=0)
    f.add("cap", "full:a", "duplicate is ignored", subject="wl:red/VM/a", weight=0.9)
    N.score(list(nodes.values()) + list(edges.values()))
    findings = f.sorted()
    graph = {"generatedAt": "now", "nodes": list(nodes.values()), "edges": list(edges.values()), "findings": findings,
             "hotspots": N.hotspots(nodes, list(edges.values())), "paths": [], "cascades": [], "changes": [],
             "namespaces": N.group_rows(list(nodes.values()), findings, [("red", "Red"), ("blue", "Blue"), ("empty", "Empty")])}
    graph.update({k: v for k, v in summarize(findings).items() if k != "findings"})
    return graph


def test_findings_set_risk_and_group_rows_count_what_is_off():
    g = _graph()
    by = {n["id"]: n for n in g["nodes"]}
    a = by["wl:red/VM/a"]
    assert a["risk"]["level"] == "critical" and a["risk"]["reasons"][0]["text"] == "a is full"
    assert a["pods"][0]["level"] == "critical"
    assert by["wl:red/VM/b"]["risk"]["level"] == "ok"                 # an info finding never colours a node
    assert [f["title"] for f in g["findings"]] == ["a is full", "b is worth knowing"]
    assert g["findings"][0]["options"][0]["title"] == "Empty it" and g["findings"][0]["patternLabel"] == "things that are full"
    rows = {r["name"]: r for r in g["namespaces"]}
    assert set(rows) == {"red", "blue"}                                  # a group with no members has no tile
    assert (rows["red"]["critical"], rows["red"]["online"], rows["blue"]["offline"]) == (1, 2, 1)
    assert rows["red"]["top"]["title"] == "a is full"
    assert g["summary"]["problems"] == 1 and g["hotspots"][0]["id"] == "wl:red/VM/a"


def test_scope_keeps_what_rim_nodes_hang_off_and_pinned_hosts():
    g = _graph()
    red = filter_graph(g, ["red"], True)
    ids = {n["id"] for n in red["nodes"]}
    assert {"wl:red/VM/a", "wl:red/VM/b", "ext:net", "ext:nic", "host:h1", "host:pinned"} <= ids
    assert "wl:blue/VM/c" not in ids                                    # another group's VM on the same network stays out
    assert "host:spare" not in ids                                       # nothing in view sits on it
    # both networks reach the same card: both lines are kept, not just the first one found
    assert {e["id"] for e in red["edges"]} >= {"wl:red/VM/a->ext:net", "ext:net->ext:nic", "ext:net2->ext:nic"}
    blue = filter_graph(g, ["blue"], True)
    assert {f["title"] for f in blue["findings"]} == set()               # red's findings stay out of blue's view
    small, cmap = collapse(filter_graph(g, None, True))
    assert small["view"]["collapsed"] is False and cmap["wl:red/VM/a"] == "wl:red/VM/a"


def test_index_tables_search_and_detail_use_the_module_hooks():
    g = _graph()
    idx = Index(g, workload_cols=lambda n: {"ip": n["thing"]["ip"], "on": n["present"]},
                search_extra=lambda n: {"prefix": [(n.get("thing") or {}).get("ip", "")], "hit": {"ip": (n.get("thing") or {}).get("ip", "")}},
                text_fields=("ip",))
    rows = idx.workload_rows()
    assert {r["name"] for r in rows} == {"a", "b", "c"} and all("ip" in r for r in rows)
    page = idx.page(rows, namespaces=None, platform=True, q="", sort="risk", desc=True, offset=0, limit=10, only_problems=False)
    assert page["rows"][0]["name"] == "a" and page["total"] == 3 and page["problems"] == 1
    ip = next(r["ip"] for r in rows if r["name"] == "b")
    assert [r["name"] for r in idx.page(rows, namespaces=None, platform=True, q=ip, sort="name", desc=False, offset=0, limit=10,
                                        only_problems=False)["rows"]] == ["b"]
    assert [h["name"] for h in idx.search(ip)] == ["b"] and idx.search(ip)[0]["ip"] == ip
    calls = idx.call_rows()
    assert {c["type"] for c in calls} == {"attached", "uplink"} and len(calls) == 5
    d = idx.detail("wl:red/VM/a")
    assert {n["id"] for n in d["nodes"]} >= {"wl:red/VM/a", "ext:net", "host:h1"} and d["findings"][0]["title"] == "a is full"
    assert idx.detail("wl:red/VM/a->ext:net")["edges"][0]["type"] == "attached" and idx.detail("nope") is None
