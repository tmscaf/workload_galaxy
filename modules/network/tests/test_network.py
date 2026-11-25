"""The collector end to end on the sample network, plus the parsers and the inventory on their own."""
import json
import time

from conftest import settle

from netgalaxy.devices import Inventory, classify, clean_name
from netgalaxy.graph import registered_domain, service_of
from netgalaxy.probes import Reachability
from netgalaxy.syslog import EventStore, parse_dhcp, parse_filterlog

BLOCK = ("<134>Nov 20 02:05:40 pfSense filterlog[2896]: 57,,,1770009021,wan0,match,block,in,4,0x0,,244,54321,0,none,6,tcp,44,"
         "203.0.113.9,192.0.2.1,33516,48622,0,S,123,,1024,,")


def by_name(graph, name):
    return next(n for n in graph["nodes"] if n["name"] == name)


def titles(graph, category=None):
    return [f["title"] for f in graph["findings"] if not category or f["category"] == category]


# ------------------------------------------------------------------ parsers
def test_filterlog_ipv4_and_ipv6():
    ev = parse_filterlog(BLOCK)
    assert (ev["action"], ev["iface"], ev["src"], ev["dport"], ev["tracker"]) == ("block", "wan0", "203.0.113.9", 48622, "1770009021")
    v6 = parse_filterlog("<134>1 2026-11-20T02:05:40+00:00 fw filterlog 9 - - 4,,,1000000103,lan0,match,block,in,6,0x00,0x00000,64,UDP,17,60,"
                         "fe80::1,ff02::fb,5353,5353,60")
    assert v6["proto"] == "udp" and v6["dst"] == "ff02::fb" and v6["dport"] == 5353
    assert parse_filterlog("dhcpd[1]: DHCPACK on 10.20.0.5 to aa:bb:cc:dd:ee:ff via lan0") is None


def test_dhcp_lease_with_and_without_name():
    assert parse_dhcp("dhcpd[1]: DHCPACK on 10.20.0.30 to de:c8:04:aa:00:30 (Sams-iPad) via lan0") == \
        {"ip": "10.20.0.30", "mac": "DE:C8:04:AA:00:30", "name": "Sams-iPad"}
    assert parse_dhcp("dhcpd[1]: DHCPACK on 10.20.0.31 to b2:9e:c2:aa:00:31 via lan0")["name"] == ""


def test_event_summary_counts_known_bad_and_scanners():
    store = EventStore()
    store.ingest(BLOCK)
    for port in range(2000, 2020):
        store.ingest(BLOCK.replace("1770009021", "1000000103").replace("203.0.113.9", "203.0.113.200").replace(",48622,", f",{port},"))
    s = store.summary(is_lan=lambda ip: ip.startswith("10.20."), wan_interfaces=["wan0"], blocklist_trackers=["1770009021"])
    assert s["blockedIn"] == 21 and s["blockedInKnownBad"] == 1 and s["blockedInSources"] == 2
    assert s["scanners"] == [{"ip": "203.0.113.200", "ports": 20}]


# ------------------------------------------------------------------ inventory
def test_names_that_are_not_names_are_dropped():
    assert clean_name("541497c2-aa5c-40c9-a67e-9a68fef71acc") == ""
    assert clean_name("$1ba50c2d-ce1b-4f16-8f64-4ae65d4fe") == ""
    assert clean_name("10.20.0.9", "10.20.0.9") == ""
    assert clean_name("studio-mac.local") == "studio-mac"


def test_classification():
    assert classify("", "VMware, Inc.", "00:0C:29:01:02:03", 3) == "vm"           # the MAC outranks "workstation"
    assert classify("april's ipad (2)", "", "", None) == "tablet"
    assert classify("NPI0A1B2C", "", "", None) == "printer"
    assert classify("", "", "DE:C8:04:AA:00:99", None) == "phone"                 # a private (random) MAC
    assert classify("", "Espressif Inc.", "CC:BA:97:00:00:01", None) == "iot"
    assert classify("", "", "", None) == "unknown"


def test_device_keeps_its_history_when_its_mac_becomes_known(tmp_path):
    inv = Inventory(str(tmp_path))
    inv.observe("10.20.0.30", now=1000.0, name="ipad", up_bps=100, down_bps=100)
    inv.observe("10.20.0.30", now=2000.0, mac="de:c8:04:aa:00:30")
    assert list(inv.devices) == ["DE:C8:04:AA:00:30"] and inv.devices["DE:C8:04:AA:00:30"]["firstSeen"] == 1000.0
    inv.save(force=True)
    assert Inventory(str(tmp_path)).describe("DE:C8:04:AA:00:30")["name"] == "ipad"


def test_site_file_names_win():
    inv = Inventory(overrides=[{"ip": "10.20.0.12", "name": "web-vm", "type": "server"}])
    inv.observe("10.20.0.12", now=1.0, name="10.20.0.12")
    d = inv.describe("10.20.0.12")
    assert (d["name"], d["group"], d["pinned"]) == ("web-vm", "servers", True)


# ------------------------------------------------------------------ services
def test_internet_services_are_named_by_app_then_domain_then_address():
    assert service_of("TLS.Netflix", "ipv4-c001.nflxvideo.net", "198.51.100.10") == ("app:netflix", "Netflix")
    assert service_of("TLS", "objects.githubusercontent.com", "198.51.100.41") == ("dom:githubusercontent.com", "githubusercontent.com")
    assert service_of("Unknown", "198.51.100.80", "198.51.100.80") == ("ip:198.51.100.80", "198.51.100.80")
    assert registered_domain("www.example.co.uk") == "example.co.uk"


# ------------------------------------------------------------------ the whole picture
def test_graph_has_devices_services_and_hosts(state):
    g = settle(state)
    kinds = {k: sum(1 for n in g["nodes"] if n["kind"] == k) for k in ("workload", "external", "host", "clients")}
    assert kinds == {"workload": 17, "external": 13, "host": 2, "clients": 1}
    tv = by_name(g, "living room tv")
    assert tv["namespace"] == "tv-media" and tv["device"]["downBps"] > tv["device"]["upBps"] > 0
    assert by_name(g, "web-vm")["pods"][0]["hostId"] == "host:hypervisor"            # a VM hangs off the hypervisor
    assert by_name(g, "studio-mac")["pods"][0]["hostId"] == "host:router"
    edge = next(e for e in g["edges"] if e["target"] == "ext:app:netflix" and "c4-dd-57" in e["source"])
    assert edge["traffic"]["bytesPerSec"] > 100_000 and edge["ports"] == ["443/TCP"] and edge["net"]["apps"] == ["Netflix"]
    assert {n["name"] for n in g["namespaces"]} >= {"servers", "phones-tablets", "smart-home"}
    json.dumps(g)                                                                     # everything serialises


def test_findings_on_the_sample_network(state):
    g = settle(state)
    for d in state.inventory.devices.values():        # an hour of history, with a modest normal upload
        d["samples"], d["upAvg"] = 200, min(d["upAvg"], 30_000)
    g = settle(state)
    assert any("known-bad address" in t and "android" in t for t in titles(g, "threat"))
    assert any(t.startswith("SSH on nas is open to the whole internet") for t in titles(g, "exposure"))
    assert any("10.20.0.77, which is not on the network" in t for t in titles(g, "exposure"))
    assert any("uploading far more than usual" in t for t in titles(g, "anomaly"))
    assert any("keeps asking for blocked names" in t for t in titles(g, "dns"))
    assert any("turned away" in t for t in titles(g, "inbound"))
    assert by_name(g, "nas")["risk"]["level"] == "critical"
    assert by_name(g, "living room tv")["risk"]["level"] == "ok"
    threat = next(f for f in g["findings"] if f["category"] == "threat")
    assert threat["options"] and all(o["title"] and o["detail"] for o in threat["options"])


def test_full_graph_keeps_pattern_members(state):
    """Many findings of one kind form a pattern in a response; the full graph must still hold each one."""
    from netgalaxy.graphops import filter_graph
    g = settle(state)
    new = [f for f in g["findings"] if f["id"].startswith("device:new:")]
    assert len(new) >= 4                                   # enough to form a pattern
    view = filter_graph(g, None, True)
    assert any(p["key"] == "device|device:new" and p["count"] == len(new) for p in view["patterns"])


def test_nothing_is_called_new_during_the_first_day(settings):
    from netgalaxy.state import NetState
    st = NetState(settings)                                # inventory created just now
    g = settle(st)
    assert not titles(g, "device") or all("New device" not in t for t in titles(g, "device"))
    assert g["changes"] == [] and g["network"]["learning"] is True


def test_a_device_that_left_is_offline_not_broken(state):
    settle(state)
    state.sample.snapshot = (lambda orig: lambda now: {**orig(now), "hosts": [h for h in orig(now)["hosts"] if not h["ip"].endswith(".60")]})(state.sample.snapshot)
    for d in state.inventory.devices.values():
        if d.get("ip", "").endswith(".60"):
            d["lastSeen"] -= 3600
    g = settle(state)
    printer = by_name(g, "hp laserjet")
    assert printer["device"]["present"] is False and printer["pods"] == [] and printer["replicas"]["desired"] == 0
    assert printer["risk"]["level"] == "ok"


def test_quiet_server_that_answers_a_check_is_online(settings):
    from netgalaxy.state import NetState
    st = NetState(settings)
    st.reach = Reachability(check=lambda ip, ports: ip == "10.20.0.77")
    st._targets = st.reach.targets_from(settings.site)
    assert st._targets["10.20.0.10"][0] == 22 and 3306 not in st._targets["10.20.0.77"]
    g = settle(st)
    assert any(n["kind"] == "workload" and n["device"]["ip"] == "10.20.0.77" and n["device"]["present"] for n in g["nodes"])
    assert not any("10.20.0.77, which is not on the network" in t for t in titles(g))


def test_ntopng_down_keeps_serving_and_says_so(state):
    settle(state)
    state._read = lambda now: "ntopng unreachable: ConnectError"
    g = state.refresh_once()
    assert state.error and any(f["id"] == "platform:ntopng" and f["severity"] == "critical" for f in g["findings"])
    assert sum(1 for n in g["nodes"] if n["kind"] == "workload") == 17


# ------------------------------------------------------------------ API
def test_api(settings):
    from fastapi.testclient import TestClient
    import importlib
    import netgalaxy.main as main
    importlib.reload(main)
    with TestClient(main.app) as client:
        for _ in range(40):
            if client.get("/readyz").status_code == 200:
                break
            time.sleep(0.25)
        meta = client.get("/api/v1/meta").json()
        assert meta["app"] == "network-galaxy" and meta["mode"] == "sample" and len(meta["groups"]) == 9
        topo = client.get("/api/v1/topology").json()
        assert topo["view"]["collapsed"] is False and len(topo["nodes"]) >= 30
        only = client.get("/api/v1/topology?ns=tv-media").json()
        assert {n["namespace"] for n in only["nodes"] if n["kind"] == "workload" and not n["ghost"]} == {"tv-media"}
        rows = client.get("/api/v1/workloads?sort=down&limit=3").json()
        assert rows["total"] == 17 and {"ip", "mac", "up", "down", "type"} <= set(rows["rows"][0])
        calls = client.get("/api/v1/calls?limit=5").json()
        assert calls["rows"] and "bps" in calls["rows"][0]
        assert client.get("/api/v1/search?q=10.20.0.3").json()["hits"]
        assert client.get("/api/v1/search?q=synology").json()["hits"][0]["name"] == "nas"
        detail = client.get("/api/v1/node", params={"id": "host:router"}).json()
        assert detail["nodes"][0]["infra"]["ip"] == "10.20.0.1"
        tri = client.get("/api/v1/triage?severity=warning").json()
        assert tri["counts"]["critical"] >= 1 and all(f["severity"] != "info" for f in tri["findings"])
        assert client.get("/api/v1/network").json()["devices"] == 16
        # Every finding survives the trip through the API: grouped ones as a pattern, the rest listed.
        full = client.get("/api/v1/triage").json()
        listed = {f["id"] for f in full["findings"]}
        grouped = sum(p["count"] for p in full["patterns"])
        assert len(listed) + grouped >= full["summary"]["findings"] - 1
        assert any(f["id"].startswith("exposure:dead:") for f in full["findings"])       # no device, still shown
        filtered = client.get("/api/v1/triage?ns=tv-media").json()
        assert any(f["id"].startswith("exposure:dead:") for f in filtered["findings"])
        assert client.get("/api/v1/demo/scenarios").status_code == 404
        assert client.get("/v/x/../../collector/netgalaxy/main.py").status_code == 404
