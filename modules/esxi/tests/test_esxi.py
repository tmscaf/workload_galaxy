# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""The ESXi module against its sample host: the graph, the checks, sizing, history and the API."""
import re
import time

from fastapi.testclient import TestClient


def titles(graph, category=None):
    return [f["title"] for f in graph["findings"] if category is None or f["category"] == category]


def node(graph, name):
    return next(n for n in graph["nodes"] if n["name"] == name)


# ---------------------------------------------------------------------------------------------- graph
def test_graph_draws_vms_host_datastores_networks_and_uplinks(graph):
    kinds = {}
    for n in graph["nodes"]:
        kinds.setdefault(n["subkind"], []).append(n["name"])
    assert len(kinds["Virtual machine"]) == 14 and kinds["Template"] == ["template-ubuntu"]
    assert kinds["ESXi host"] == ["esx-lab-01.example.net"]
    assert sorted(kinds["Datastore"]) == ["bulk-01", "ssd-01"]
    assert sorted(kinds["Port group"]) == ["DMZ", "Lab trunk", "Management Network", "VM Network"]
    assert sorted(kinds["Uplink"]) == ["vmnic0", "vmnic1", "vmnic2"]          # vmnic3 is on no virtual switch
    types = {e["type"] for e in graph["edges"]}
    assert types == {"attached", "stores", "uplink"}
    db = node(graph, "db-01")
    assert db["namespace"] == "linux" and db["vm"]["cpus"] == 8 and db["vm"]["on"] is True
    # a running VM has its slot on the host and one entry per disk on that disk's datastore
    assert [(p["part"], p["hostId"]) for p in db["pods"]] == [("compute", "host:esx-lab-01-example-net"), ("disk", "ext:ds:ssd-01"),
                                                             ("disk", "ext:ds:bulk-01")]
    assert {e["target"] for e in graph["edges"] if e["source"] == db["id"]} == {"ext:net:vm-network", "ext:ds:ssd-01", "ext:ds:bulk-01"}


def test_powered_off_vm_is_dimmed_not_failing_and_keeps_its_disks(graph):
    off = node(graph, "win10-test")
    assert off["present"] is False and off["replicas"]["desired"] == 0 and off["risk"]["level"] == "ok"
    assert [p["part"] for p in off["pods"]] == ["disk"]
    e = next(e for e in graph["edges"] if e["source"] == off["id"] and e["type"] == "stores")
    assert e["evidence"] == [] and e["traffic"]["bytesPerSec"] == 0             # drawn dashed, nothing flowing
    rows = {r["name"]: r for r in graph["namespaces"]}
    assert rows["windows"]["offline"] == 1 and rows["windows"]["online"] == 3


def test_estate_totals_add_up(graph):
    e = graph["estate"]
    assert (e["vms"], e["on"], e["off"], e["cores"]) == (15, 12, 3, 12)
    assert e["vcpus"] == sum(n["vm"]["cpus"] for n in graph["nodes"] if n["kind"] == "workload" and n["vm"]["on"])
    assert 0.9 < e["memPct"] < 1.0 and e["snapshots"]["count"] == 3 and e["storage"]["datastores"] == 2
    bulk = node(graph, "bulk-01")["ds"]
    assert bulk["usedRatio"] > 0.9 and bulk["provisionedRatio"] > 1.0 and bulk["offBytes"] > 0 and bulk["snapshotBytes"] > 0


def test_groups_come_from_the_site_file_when_there_is_one(settings, site_file):
    from esxigalaxy.config import load_settings
    from esxigalaxy.state import EsxiState
    s = load_settings({"mode": "sample", "site_file": site_file({"groups": [
        {"id": "web", "title": "Web", "match": ["web-*", "proxy-*"]}, {"id": "data", "title": "Data", "match": ["db-*", "cache-*"]}]})})
    g = EsxiState(s).refresh_once()
    assert node(g, "web-02")["namespace"] == "web" and node(g, "db-01")["namespace"] == "data"
    assert node(g, "mail-01")["namespace"] == "linux"                           # unmatched VMs fall back to guest OS
    assert [r["title"] for r in g["namespaces"]][:2] == ["Web", "Data"]


# ---------------------------------------------------------------------------------------------- checks
def test_every_kind_of_trouble_in_the_sample_is_found(graph):
    t = titles(graph)
    for expected in (
        "esx-lab-01.example.net is using 9",                    # host memory
        "mail-01 is having memory taken back by the host",
        "build-01 spends ", "mail-01 is waiting up to 31 ms for its disk",
        "Datastore bulk-01 is 9", "Datastore bulk-01 has promised",
        "db-01 has 2 snapshots, the oldest 41 days old", "old-wiki has a snapshot 13 months old",
        "legacy-app needs its disks consolidated", "build-01: / is 96% full inside the guest",
        "win10-test is powered off and holds 120 GB",
        "web-02 still has an ISO image attached", "legacy-app runs without VMware Tools", "dc-01: VMware Tools is out of date",
        "monitor-01 has a network adapter that is not connected",
        "vmnic2 linked at 100 Mbps", "vSwitch1 has a single uplink (vmnic2)",
        "Port group Lab trunk lets VMs see each other's traffic", "Port group Lab trunk carries every VLAN",
        "SSH is running on esx-lab-01.example.net", "accepts management connections from any address",
        "clock is 7 min fast", "ESXi 7.0 is past its end of general support",
        "Hardware sensor: System Board 1 Fan 2 is degraded",
        "cache-01 is allocated 16 GB",
    ):
        assert any(expected in x for x in t), expected
    assert not any("template-ubuntu is powered off" in x for x in t)             # a template is meant to be off
    assert not any("vSwitch0 has a single uplink" in x for x in t)               # it has two


def test_findings_colour_what_they_are_about(graph):
    assert node(graph, "bulk-01")["risk"]["level"] == "critical"
    assert node(graph, "Lab trunk")["risk"]["level"] == "warning"
    assert node(graph, "vmnic2")["risk"]["level"] == "warning"
    assert node(graph, "esx-lab-01.example.net")["risk"]["level"] == "critical"
    assert node(graph, "web-01")["risk"]["level"] == "ok"
    assert node(graph, "cache-01")["risk"]["level"] == "ok"                      # sizing advice is information, never a colour


def test_memory_finding_offers_sized_ways_to_fix_it(graph):
    f = next(f for f in graph["findings"] if f["id"].startswith("capacity:memory:"))
    first = f["options"][0]
    assert first["title"].startswith("Take back about") and re.search(r"cache-01 16 GB → [23] GB", first["detail"])
    assert "check inside the guest" in f["optionsNote"]
    assert any(o["title"] == "Add memory to the host" for o in f["options"])


def test_sizing_suggestions_are_cautious():
    from esxigalaxy.findings import suggest_memory_mb, suggest_vcpus
    assert suggest_memory_mb(16384, 600) == 3072             # four times the peak, rounded up to a GB
    assert suggest_memory_mb(8192, 80) == 2048               # never under 2 GB
    assert suggest_memory_mb(4096, 80) == 2048
    assert suggest_memory_mb(4096, 700) is None              # would not halve it: not worth a shutdown
    assert suggest_memory_mb(2048, 10) is None
    assert suggest_vcpus(8, 0.05) == 1 and suggest_vcpus(8, 0.25) == 4
    assert suggest_vcpus(8, 0.5) is None and suggest_vcpus(2, 0.01) == 1


def test_sizing_waits_until_a_vm_has_been_watched_for_an_hour(settings):
    from esxigalaxy.state import EsxiState
    st = EsxiState(settings)
    st.refresh_once()
    st.history.started = time.time()
    st.history.first_seen = {k: time.time() for k in st.history.first_seen}
    g = st.refresh_once()
    assert not titles(g, "sizing")
    f = next(f for f in g["findings"] if f["id"].startswith("capacity:memory:"))
    assert not any(o["title"].startswith("Take back") for o in f["options"]) and "only just started watching" in f["optionsNote"]


def test_thresholds_are_settings(settings):
    from esxigalaxy.config import load_settings
    from esxigalaxy.state import EsxiState
    g = EsxiState(load_settings({"mode": "sample", "datastore_warn_pct": 40, "snapshot_days": 60, "memory_warn_pct": 99})).refresh_once()
    t = titles(g)
    assert any("Datastore ssd-01 is" in x for x in t)                           # 44% used now counts
    assert not any("is using 9" in x for x in t)                                # the host memory warning is off
    snap = next(f for f in g["findings"] if f["id"].startswith("storage:snapshot:") and "db-01" in f["title"])
    assert snap["severity"] == "warning"                                        # still flagged for its size (96 GB)


# ---------------------------------------------------------------------------------------------- vsphere helpers
def test_guest_family_and_duplicate_filesystems():
    from esxigalaxy.graph import _filesystems, gb
    from esxigalaxy.vsphere import guest_family
    assert guest_family("ubuntu64Guest", "Ubuntu Linux (64-bit)") == "linux"
    assert guest_family("windows2019srv_64Guest", "") == "windows"
    assert guest_family("freebsd13_64Guest", "") == "unix" and guest_family("", "") == "other"
    disks = [{"path": "/var/lib/kubelet/pods/abc/volume", "capacity": 100, "free": 40}, {"path": "/", "capacity": 100, "free": 40},
             {"path": "/boot", "capacity": 2, "free": 1}]
    assert [d["path"] for d in _filesystems(disks)] == ["/", "/boot"]             # the bind mount of / is the same filesystem
    assert (gb(4 * 2**30), gb(1.5 * 2**30), gb(96 * 2**30), gb(300 * 2**20), gb(2048 * 2**30)) == ("4 GB", "1.5 GB", "96 GB", "300 MB", "2.00 TB")


# ---------------------------------------------------------------------------------------------- history
def test_history_reports_what_changed_between_readings(tmp_path):
    from esxigalaxy.history import History
    from esxigalaxy.sample import SampleHost
    now = time.time()
    snap = SampleHost().snapshot(now)
    h = History(str(tmp_path))
    h.update(snap, now)
    assert not any(e["kind"] == "power" for e in h.events)                       # nothing was just booted
    vm = next(v for v in snap["vms"] if v["name"] == "web-01")
    vm["powerState"] = "poweredOff"
    next(v for v in snap["vms"] if v["name"] == "db-01")["snapshots"].append(
        {"name": "before-patch", "description": "", "created": now, "depth": 2, "memory": False, "quiesced": False})
    next(v for v in snap["vms"] if v["name"] == "cache-01")["memMB"] = 4096
    snap["vms"] = [v for v in snap["vms"] if v["name"] != "old-wiki"]
    h.update(snap, now + 30)
    got = {e["title"]: e for e in h.events}
    assert "web-01 was powered off" in got and "Snapshot taken on db-01" in got and "old-wiki was removed from the host" in got
    assert got["cache-01 was resized"]["detail"] == "4 vCPU, 16 GB → 4 vCPU, 4 GB"
    h.save(force=True)
    again = History(str(tmp_path))                                               # survives a restart
    assert {e["title"] for e in again.events} == set(got) and again.stats(vm["id"])["peakMemActiveMB"] > 0
    listed = again.changes(now + 60, {vm["id"]: "wl:linux/VM/web-01"}, {"wl:linux/VM/web-01": {"namespace": "linux"}})
    assert listed[0]["ageS"] == 30 and any(c["subject"] == "wl:linux/VM/web-01" for c in listed)


# ---------------------------------------------------------------------------------------------- API
def test_api(settings):
    from esxigalaxy.main import create_app
    with TestClient(create_app(settings)) as c:
        for _ in range(100):
            if c.get("/readyz").status_code == 200:
                break
            time.sleep(0.05)
        meta = c.get("/api/v1/meta").json()
        assert meta["app"] == "esxi-galaxy" and meta["mode"] == "sample" and meta["estate"]["name"] == "esx-lab-01.example.net"
        assert meta["notConfigured"] == [] and {x["id"] for x in meta["categories"]} >= {"capacity", "storage", "sizing"}
        mod = c.get("/api/v1/module").json()
        fields = {f["name"]: f for f in mod["settings"]}
        assert mod["id"] == "esxi" and fields["password"]["secret"] and fields["password"]["default"] is None
        assert fields["host"]["env"] == "ESXIGALAXY_HOST" and fields["host"]["required"]
        topo = c.get("/api/v1/topology").json()
        assert len(topo["nodes"]) == 24 and topo["view"]["collapsed"] is False
        assert "Management Network" not in {n["name"] for n in topo["nodes"]}     # no VM uses it; it is listed on the host
        assert {n["name"] for n in topo["nodes"] if n.get("nic")} == {"vmnic0", "vmnic1", "vmnic2"}
        only = c.get("/api/v1/topology?ns=windows").json()
        assert {n["name"] for n in only["nodes"] if n["kind"] == "workload"} == {"dc-01", "files-01", "legacy-app", "win10-test"}
        assert any(n["kind"] == "host" for n in only["nodes"])
        rows = c.get("/api/v1/workloads?sort=memMB&desc=true&limit=3").json()
        assert rows["total"] == 15 and [r["name"] for r in rows["rows"]][0] == "db-01" and rows["rows"][0]["memMB"] == 32768
        assert c.get("/api/v1/workloads?q=windows").json()["total"] == 4          # the filter box searches the guest OS
        calls = c.get("/api/v1/calls").json()
        assert {r["type"] for r in calls["rows"]} == {"attached", "stores", "uplink"}
        # the table lists every attachment; the map leaves out the management network's two uplinks (no VM is on it)
        assert calls["total"] == 40 and len(topo["edges"]) == 38
        ip = next(n for n in topo["nodes"] if n["name"] == "db-01")["vm"]["ip"]
        assert c.get(f"/api/v1/search?q={ip}").json()["hits"][0]["name"] == "db-01"
        detail = c.get("/api/v1/node", params={"id": "wl:linux/VM/db-01"}).json()
        assert detail["at"] == "wl:linux/VM/db-01" and any("snapshots" in f["title"] for f in detail["findings"])
        assert c.get("/api/v1/node", params={"id": "wl:linux/VM/nope"}).status_code == 404
        tri = c.get("/api/v1/triage?severity=warning").json()
        assert tri["counts"]["critical"] >= 3 and all(f["severity"] != "info" for f in tri["findings"]) and tri["estate"]["vms"] == 15
        assert c.get("/api/v1/estate").json()["on"] == 12
        page = c.get("/")
        assert page.status_code == 200 and "ESXi Galaxy" in page.text and "/js/main.js" in page.text
        assert c.get("/v/x/../../collector/esxigalaxy/config.py").status_code == 404


def test_live_mode_without_a_host_says_what_is_missing(monkeypatch):
    for k in ("ESXIGALAXY_HOST", "ESXIGALAXY_USER", "ESXIGALAXY_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    from esxigalaxy.config import load_settings
    from esxigalaxy.main import create_app
    from esxigalaxy.state import EsxiState
    s = load_settings({"mode": "live"})
    st = EsxiState(s)
    g = st.refresh_once()
    assert "no host configured" in st.error and g["nodes"] == []
    assert any(f["title"] == "The host cannot be read" for f in g["findings"])
    with TestClient(create_app(s)) as c:
        assert c.get("/api/v1/meta").json()["notConfigured"] == ["Host address", "User name", "Password"]


def test_placeholder_hardware_names_are_dropped():
    from esxigalaxy.graph import _real
    assert _real("System manufacturer") == "" and _real("To Be Filled By O.E.M.") == "" and _real("Supermicro") == "Supermicro"


# ---------------------------------------------------------------------------------------------- settings from the suite
def _app(tmp_path, **env):
    from esxigalaxy.main import create_app
    return create_app(environ={"ESXIGALAXY_MODE": "sample", "ESXIGALAXY_DATA_DIR": str(tmp_path), **env})


def _ready(c):
    for _ in range(100):
        if c.get("/readyz").status_code == 200:
            return
        time.sleep(0.05)


def test_settings_need_the_token_and_apply_without_a_restart(tmp_path):
    auth = {"Authorization": "Bearer s3cret-token"}
    with TestClient(_app(tmp_path, ESXIGALAXY_SETTINGS_TOKEN="s3cret-token")) as c:
        _ready(c)
        assert c.get("/api/v1/settings").status_code == 401                         # values are for the suite only
        assert c.put("/api/v1/settings", json={"values": {"name": "x"}}).status_code == 401
        assert c.put("/api/v1/settings", json={"values": {"name": "x"}}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        form = c.get("/api/v1/settings", headers=auth).json()
        names = [f["name"] for f in form["fields"]]
        assert form["writable"] and form["persistent"] and form["missing"] == []      # sample data needs no host
        assert "password" in names and "groups" in names
        assert not {"settings_token", "data_dir", "web_root", "log_level", "site_file"} & set(names)
        assert c.get("/api/v1/meta").json()["estate"]["name"] == "esx-lab-01.example.net"
        saved = c.put("/api/v1/settings", headers=auth, json={"values": {"name": "Lab one", "groups": "Web: web-*, proxy-*"}}).json()
        assert saved["saved"] and saved["forgot"] == []
        assert {f["name"]: f for f in saved["fields"]}["name"]["source"] == "saved"
        _ready(c)
        for _ in range(100):                                                         # the new reading replaces the old one
            if c.get("/api/v1/meta").json()["estate"]["name"] == "Lab one":
                break
            time.sleep(0.05)
        assert c.get("/api/v1/meta").json()["estate"]["name"] == "Lab one"
        groups = {g["title"] for g in c.get("/api/v1/namespaces").json()["namespaces"]}
        assert "Web" in groups and "Linux VMs" in groups
        assert c.post("/api/v1/settings/test", headers=auth, json={"values": {}}).json()["ok"] is True
        missing = c.post("/api/v1/settings/test", headers=auth, json={"values": {"mode": "live", "host": "esx.example"}}).json()
        assert missing["ok"] is False and "User name" in missing["message"] and "Password" in missing["message"]
        assert c.put("/api/v1/settings", headers=auth, json={"values": {"refresh_seconds": "soon"}}).status_code == 400
        assert c.put("/api/v1/settings", headers=auth, json={"values": {"settings_token": "mine"}}).status_code == 400
    with TestClient(_app(tmp_path, ESXIGALAXY_SETTINGS_TOKEN="s3cret-token")) as c:   # a restart keeps what was saved
        _ready(c)
        assert c.get("/api/v1/meta").json()["estate"]["name"] == "Lab one"


def test_without_a_token_settings_cannot_be_read_or_changed(tmp_path):
    with TestClient(_app(tmp_path, ESXIGALAXY_HOST="esx.example", ESXIGALAXY_USER="ro")) as c:
        _ready(c)
        form = c.get("/api/v1/settings").json()
        assert form["writable"] is False and all(f["value"] is None and f["set"] is None for f in form["fields"])
        assert "esx.example" not in str(form)
        assert c.put("/api/v1/settings", json={"values": {"name": "x"}}).status_code == 403
        assert c.post("/api/v1/settings/test", json={"values": {}}).status_code == 403


def test_groups_setting_is_read_one_group_per_line():
    from esxigalaxy.config import parse_groups
    assert parse_groups("Web servers: web-*, proxy-*\n\nno colon here\nEmpty:\n Data : db-*") == [
        {"id": "web-servers", "title": "Web servers", "match": ["web-*", "proxy-*"]}, {"id": "data", "title": "Data", "match": ["db-*"]}]
