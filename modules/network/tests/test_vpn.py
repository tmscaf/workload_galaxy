# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""VPN sessions: the router's status report, the VPN log, the map and the API."""
import json

from conftest import settle

LOG = [
    "<29>Nov 21 02:18:01 pfSense openvpn[62851]: 192.168.1.195:63149 peer info: IV_PLAT=mac",
    "<29>Nov 21 02:18:01 pfSense openvpn[62851]: 192.168.1.195:63149 peer info: IV_GUI_VER=OCmacOS_3.8.1-5790",
    "<29>Nov 21 02:18:01 pfSense openvpn[62851]: 192.168.1.195:63149 [alex] Peer Connection Initiated with [AF_INET]192.168.1.195:63149",
    "<29>Nov 21 02:18:04 pfSense openvpn[62851]: alex/192.168.1.195:63149 MULTI_sva: pool returned IPv4=10.8.0.2, IPv6=(Not enabled)",
    "<29>Nov 21 02:18:05 pfSense openvpn[35470]: openvpn server 'ovpns1' user 'alex' address '192.168.1.195:63149' - connected",
]


def tracker(tmp_path):
    from netgalaxy.vpn import VpnTracker
    return VpnTracker(str(tmp_path), home_cidrs=["192.168.1.0/24"], public_address="198.51.100.7")


def report(user="alex", remote="192.168.1.195:63149", tunnel="10.8.0.2", recv=1000, sent=5000, since=1000):
    return {"servers": [{"name": "Home VPN UDP4:1194", "port": "1194", "conns": [
        {"common_name": "alex", "user_name": user, "remote_host": remote, "virtual_addr": tunnel, "bytes_recv": recv,
         "bytes_sent": sent, "connect_time_unix": since, "cipher": "AES-256-GCM"}]}]}


def test_log_lines_make_a_session_at_once(tmp_path):
    v = tracker(tmp_path)
    understood = [v.ingest_log(line, ts=2000 + i) for i, line in enumerate(LOG)]
    assert understood == [True, True, False, True, True]          # "Peer Connection Initiated" adds nothing
    snap = v.snapshot(now=2010)
    assert snap["connected"] == 1
    s = snap["sessions"][0]
    assert (s["user"], s["real"], s["tunnel"], s["platform"], s["app"]) == ("alex", "192.168.1.195", "10.8.0.2", "Mac",
                                                                           "OpenVPN Connect 3.8.1")
    assert s["where"] == "inside your network" and not s["confirmed"]
    assert snap["events"][0]["kind"] == "connected"


def test_report_confirms_measures_and_ends_sessions(tmp_path):
    v = tracker(tmp_path)
    for line in LOG:
        v.ingest_log(line, ts=2000)
    assert v.ingest_status(report(), now=2030) == 1
    assert v.ingest_status(report(recv=1000 + 60 * 1000, sent=5000 + 60 * 10_000), now=2090) == 1
    s = v.snapshot(now=2090)["sessions"][0]
    assert s["confirmed"] and s["platform"] == "Mac"
    assert s["inBps"] == 8000 and s["outBps"] == 80_000             # bits per second, from the change in totals
    assert s["durationS"] == 1090
    # Gone from the report: disconnected, once.
    v.ingest_status({"servers": [{"name": "Home VPN", "conns": []}]}, now=2150)
    snap = v.snapshot(now=2150)
    assert snap["connected"] == 0
    assert [e["kind"] for e in snap["events"]].count("disconnected") == 1
    # Persisted: a restart remembers the history.
    assert tracker(tmp_path).snapshot(now=2200)["events"][0]["kind"] == "disconnected"


def test_disconnect_line_ends_a_session_and_failures_are_counted(tmp_path):
    v = tracker(tmp_path)
    for line in LOG:
        v.ingest_log(line, ts=2000)
    v.ingest_log("<29>pfSense openvpn[39384]: openvpn server 'ovpns1' user 'alex' address '192.168.1.195:63149' - disconnected", ts=2100)
    v.ingest_log("<29>pfSense openvpn[3799]: user 'guest' could not authenticate.", ts=2110)
    v.ingest_log("<29>pfSense openvpn[62851]: TLS Error: cannot locate HMAC in incoming packet from [AF_INET]203.0.113.40:49769", ts=2120)
    snap = v.snapshot(now=2130)
    assert snap["connected"] == 0
    assert snap["events"][0]["kind"] == "failed" and snap["events"][0]["user"] == "guest"
    assert snap["events"][1]["kind"] == "disconnected" and snap["events"][1]["duration"] == 100
    assert snap["failed24h"] == 1 and snap["probes24h"] == 1 and snap["probeSources"][0]["ip"] == "203.0.113.40"


def test_unconfirmed_log_session_expires(tmp_path):
    v = tracker(tmp_path)
    for line in LOG:
        v.ingest_log(line, ts=2000)
    assert v.snapshot(now=2000 + 901)["connected"] == 0


def test_bad_report_is_refused(tmp_path):
    import pytest
    v = tracker(tmp_path)
    with pytest.raises(ValueError):
        v.ingest_status({"nope": 1})


def test_vpn_session_on_the_map(state):
    g = settle(state)
    vpn = [n for n in g["nodes"] if n.get("vpn")]
    assert len(vpn) == 1
    n = vpn[0]
    assert n["namespace"] == "vpn" and n["name"] == "sam · iPhone / iPad" and n["pods"][0]["hostId"] == "host:router"
    assert n["vpn"]["app"] == "OpenVPN Connect 3.4.2" and n["vpn"]["real"] == "198.51.100.200"
    # Its traffic through the tunnel is drawn to the device it reaches, not as the internet coming in.
    nas = next(x["id"] for x in g["nodes"] if x["kind"] == "workload" and x["device"]["ip"] == "10.20.0.10")
    assert any(e["source"] == n["id"] and e["target"] == nas and e["type"] == "calls" for e in g["edges"])
    assert not any(e["source"] == "clients" and e["target"] == nas and "observed" in e["evidence"] and e["traffic"]["bytes"] == 30_000_000
                   for e in g["edges"])
    # Never reported as a new or unusual device.
    assert not any(f.get("subject") == n["id"] for f in g["findings"])
    assert not any(c["subject"] == n["id"] for c in g["changes"])
    assert g["network"]["vpn"]["connected"] == 1 and g["network"]["vpn"]["failed24h"] == 1
    assert g["sources"]["vpn"]["connected"] == 1


def test_vpn_api_and_router_report(settings, monkeypatch):
    monkeypatch.setenv("NETGALAXY_BLOCKLIST_TOKEN", "t0ken")
    monkeypatch.setenv("NETGALAXY_BLOCKLIST_SENDERS", "testclient")
    import importlib
    import netgalaxy.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        assert c.get("/api/v1/vpn").json()["connected"] == 1        # the sample session
        body = json.dumps(report(user="pat", remote="198.51.100.9:4000", tunnel="10.30.0.3"))
        assert c.post("/api/v1/vpn-status", content=body).status_code == 403                       # no token
        assert c.post("/api/v1/vpn-status", content=body, headers={"X-NetGalaxy-Token": "wrong"}).status_code == 403
        assert c.post("/api/v1/vpn-status", content="{]", headers={"X-NetGalaxy-Token": "t0ken"}).status_code == 400
        r = c.post("/api/v1/vpn-status", content=body, headers={"X-NetGalaxy-Token": "t0ken"})
        assert r.status_code == 200 and r.json()["sessions"] == 1
        snap = c.get("/api/v1/vpn").json()
        assert [s["user"] for s in snap["sessions"]] == ["pat"]
