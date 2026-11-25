"""Wi-Fi and connection health: the pinger's bookkeeping, the UniFi reader, the diagnosis, the map and the API."""
import json

import httpx
import pytest

from conftest import settle

BELL, IPAD, PHONE, SONOS, ANDROID = ("4E:96:09:AA:00:50", "DE:C8:04:AA:00:30", "B2:9E:C2:AA:00:31", "18:7F:88:AA:00:41",
                                     "7E:B7:71:AA:00:32")
AP1, AP2 = "F4:92:BF:AA:00:70", "F4:92:BF:AA:00:71"


# ---------------------------------------------------------------------------------------------- the pinger
def test_echo_packets_round_trip():
    from netgalaxy.pinger import dns_query, echo_request, parse_reply
    req = echo_request(77, 513, b"tokentok")
    assert req[0] == 8 and len(req) == 16
    reply = b"\x00" + req[1:]                                   # an echo reply carries the request back
    assert parse_reply(reply) == (513, b"tokentok")
    ip_header = bytes([0x45]) + bytes(19)
    assert parse_reply(ip_header + reply) == (513, b"tokentok")     # macOS hands over the IP header too
    assert parse_reply(req) is None                             # our own request is not an answer
    q = dns_query("example.com", 7)
    assert q[:2] == b"\x00\x07" and b"\x07example\x03com\x00" in q


def test_series_statistics_history_and_outages():
    from netgalaxy.pinger import Series
    s = Series()
    t0 = 1_000_020.0
    for i in range(60):                                         # five minutes, one ping every 5 s, every tenth lost
        s.add(t0 + i * 5, None if i % 10 == 9 else 10.0 + (i % 3))
    st = s.stats(t0 + 300)
    assert st["sent"] == 60 and st["lossPct"] == 10.0 and st["p50Ms"] == 11.0 and st["lastAnswerS"] == 10
    hist = s.history(t0 + 300, span=3600, step=60)
    assert len(hist) == 5 and hist[0][3] == pytest.approx(8.3, abs=0.1) and hist[0][1] == pytest.approx(10.9, abs=0.2)
    for i in range(36):                                         # then three minutes of silence
        s.add(t0 + 300 + i * 5, None)
    assert s.stats(t0 + 480, window=120)["lossPct"] == 100.0
    out = s.outages(t0 + 480)
    assert len(out) == 1 and out[0]["minutes"] == 3


def test_history_survives_a_restart(tmp_path):
    from netgalaxy.pinger import Pinger
    import time
    p = Pinger(str(tmp_path))
    now = time.time()
    for i in range(20):
        p.record("router", now - 100 + i * 5, 0.5)
    p.set_targets({"router": ("10.20.0.1", "fast")})
    p.save(force=True)
    again = Pinger(str(tmp_path))
    assert again.history("router", now, 3600, 60) and again.stats("router", now)["lastAnswerS"] is not None


def test_hop_thresholds():
    from netgalaxy.wifi import hop_state
    assert hop_state("router", None)[0] == "unknown"
    assert hop_state("router", {"sent": 60, "lossPct": 0.0, "p50Ms": 0.4})[0] == "ok"
    assert hop_state("router", {"sent": 60, "lossPct": 0.0, "p50Ms": 25.0})[0] == "warn"
    assert hop_state("internet", {"sent": 60, "lossPct": 0.0, "p50Ms": 25.0})[0] == "ok"
    assert hop_state("internet", {"sent": 60, "lossPct": 5.0, "p50Ms": 25.0})[0] == "warn"
    assert hop_state("internet", {"sent": 60, "lossPct": 30.0, "p50Ms": 25.0})[0] == "bad"
    assert hop_state("gear", {"sent": 60, "lossPct": 100.0, "p50Ms": None, "lastAnswerS": 400})[0] == "down"
    assert hop_state("sta", {"sent": 2, "lossPct": 100.0, "p50Ms": None})[0] == "unknown"      # two pings prove nothing


# ---------------------------------------------------------------------------------------------- the UniFi reader
def controller(calls, password="right", expire_after=None):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path == "/api/login":
            ok = json.loads(req.content)["password"] == password
            return httpx.Response(200 if ok else 400, json={"meta": {"rc": "ok" if ok else "error"}, "data": []},
                                  headers={"Set-Cookie": "unifises=abc; Path=/"} if ok else {})
        if expire_after is not None and len([c for c in calls if c[1].endswith("stat/device")]) == expire_after and not handler.expired:
            handler.expired = True
            return httpx.Response(401, json={"meta": {"rc": "error", "msg": "api.err.LoginRequired"}})
        data = {"stat/device": [{"mac": "aa:bb:cc:00:00:01", "type": "uap"}], "stat/sta": [{"mac": "aa:bb:cc:00:00:02"}],
                "stat/health": [], "stat/event": [], "stat/sysinfo": [{"version": "7.2.95"}]}
        return httpx.Response(200, json={"meta": {"rc": "ok"}, "data": data[req.url.path.split("/default/")[1]]})
    handler.expired = False
    return httpx.MockTransport(handler)


def test_unifi_reader_signs_in_reads_and_signs_in_again_when_the_session_ends():
    from netgalaxy.unifi import Unifi
    calls = []
    u = Unifi("https://c:8443", "viewer", "right", transport=controller(calls, expire_after=2))
    snap = u.snapshot()
    assert snap["version"] == "7.2.95" and len(snap["devices"]) == 1 and len(snap["clients"]) == 1
    assert calls[0] == ("POST", "/api/login") and ("POST", "/api/s/default/stat/event") in calls
    u.snapshot()                                                # the second read meets a 401 and signs in once more
    assert [c for c in calls if c[1] == "/api/login"] == [("POST", "/api/login")] * 2
    assert all(m == "GET" or p.endswith(("/api/login", "stat/event")) for m, p in calls)       # nothing else is ever written


def test_unifi_reader_does_not_hammer_a_refused_login():
    from netgalaxy.unifi import Unifi, UnifiError
    calls = []
    u = Unifi("https://c:8443", "viewer", "wrong", transport=controller(calls))
    for _ in range(3):
        with pytest.raises(UnifiError, match="refused the login"):
            u.snapshot()
    assert len(calls) == 1                                      # one attempt, then it waits


# ---------------------------------------------------------------------------------------------- the diagnosis
def tracker(tmp_path=None, **kw):
    from netgalaxy.pinger import Pinger
    from netgalaxy.wifi import WifiTracker
    p = Pinger(str(tmp_path) if tmp_path else "", dns_server="10.20.0.1")
    return WifiTracker(p, str(tmp_path) if tmp_path else "", router_ip="10.20.0.1", isp_gateway="192.0.2.254",
                       internet=["1.1.1.1", "8.8.8.8"], configured=True, **kw), p


def feed(p, now, ms):
    """Five minutes of pings per target: a number is the round trip in ms, None is silence, a tuple is (ms, share lost)."""
    for tid, v in ms.items():
        for i in range(60):
            lost = v is None or (isinstance(v, tuple) and i % round(1 / v[1]) == 0)
            p.record(tid, now - 300 + i * 5, None if lost else (v[0] if isinstance(v, tuple) else v))


def raw_client(mac, ap=AP1, signal=-55, radio="na", retries=10, attempts=1000, assoc=1000, uptime=5000, **extra):
    return {"mac": mac.lower(), "hostname": extra.pop("hostname", "dev-" + mac[-2:]), "ip": "10.20.0." + str(int(mac[-2:], 16)), "is_wired": False,
            "ap_mac": ap.lower(), "essid": "Home", "radio": radio, "radio_proto": "ax", "channel": 36 if radio == "na" else 6,
            "signal": signal, "tx_rate": 400000, "rx_rate": 300000, "tx_retries": retries, "wifi_tx_attempts": attempts,
            "latest_assoc_time": assoc, "uptime": uptime, "idletime": 1, **extra}


def raw_ap(mac, name, util=20, state=1, uplink=None, uptime=90000, channel=36):
    return {"mac": mac.lower(), "type": "uap", "name": name, "ip": "10.20.0." + str(int(mac[-2:], 16)), "state": state, "adopted": True,
            "uptime": uptime, "num_sta": 2, "uplink": uplink or {"type": "wire", "speed": 1000, "full_duplex": True},
            "radio_table": [{"name": "wifi0", "radio": "na", "channel": channel, "ht": 80}],
            "radio_table_stats": [{"name": "wifi0", "radio": "na", "channel": channel, "cu_total": util, "cu_self_rx": 3, "cu_self_tx": 3, "num_sta": 2}]}


HEALTHY = {"router": 0.4, "gateway": 3.0, "inet:1.1.1.1": 14.0, "inet:8.8.8.8": 16.0, "dns": 9.0}


def test_upstream_names_the_hop_that_breaks():
    w, p = tracker()
    now = 2_000_000.0
    feed(p, now, HEALTHY)
    assert w.upstream(now)["level"] == "ok"
    w, p = tracker()
    feed(p, now, {**HEALTHY, "gateway": (3.0, 0.25), "inet:1.1.1.1": (14.0, 0.25), "inet:8.8.8.8": (16.0, 0.25)})
    up = w.upstream(now)
    assert (up["level"], up["where"]) == ("bad", "isp") and "line" in up["title"]
    w, p = tracker()
    feed(p, now, {**HEALTHY, "inet:1.1.1.1": None})            # one public host ignoring pings is not an outage
    assert w.upstream(now)["level"] == "ok"
    w, p = tracker()
    feed(p, now, {**HEALTHY, "inet:1.1.1.1": None, "inet:8.8.8.8": None})
    assert w.upstream(now)["where"] == "internet"
    w, p = tracker()
    feed(p, now, {**HEALTHY, "router": (0.4, 0.5)})
    assert w.upstream(now)["where"] == "router"
    w, p = tracker()
    feed(p, now, {**HEALTHY, "dns": 400.0})
    assert w.upstream(now)["where"] == "dns"


def test_each_kind_of_wifi_trouble_is_told_apart():
    w, p = tracker()
    now = 2_000_000.0
    feed(p, now, HEALTHY)
    aps = [raw_ap(AP1, "Hall AP"), raw_ap(AP2, "Busy AP", util=85)]
    w.ingest({"devices": aps, "clients": [
        raw_client("AA:00:00:00:00:10"),                                         # fine
        raw_client("AA:00:00:00:00:11", signal=-84),                             # too far away
        raw_client("AA:00:00:00:00:12", ap=AP2),                                 # on a crowded channel
        raw_client("AA:00:00:00:00:13", attempts=1000, retries=100),
    ]}, now - 30)
    w.ingest({"devices": aps, "clients": [
        raw_client("AA:00:00:00:00:10", attempts=2000, retries=30),
        raw_client("AA:00:00:00:00:11", signal=-84, attempts=2000, retries=60),
        raw_client("AA:00:00:00:00:12", ap=AP2, attempts=2000, retries=40),
        raw_client("AA:00:00:00:00:13", attempts=2000, retries=500),             # 40% of new frames re-sent, strong signal
    ]}, now)
    by = {c["mac"][-2:]: c for c in w.snapshot(now)["clients"]}
    assert (by["10"]["diag"]["level"], by["10"]["diag"]["where"]) == ("ok", "none") and by["10"]["retryPct"] == 2.0
    assert (by["11"]["diag"]["level"], by["11"]["diag"]["where"]) == ("bad", "signal")
    assert (by["12"]["diag"]["level"], by["12"]["diag"]["where"]) == ("warn", "air") and "crowded" in by["12"]["diag"]["title"]
    assert by["13"]["retryPct"] == 40.0 and (by["13"]["diag"]["level"], by["13"]["diag"]["where"]) == ("bad", "air")
    assert "noisy link" in by["13"]["diag"]["title"]
    assert [c["mac"][-2:] for c in w.snapshot(now)["clients"]][-1] == "10"       # worst first, healthy last


def test_a_failing_access_point_outranks_its_devices_and_the_line_outranks_nothing_local():
    w, p = tracker()
    now = 2_000_000.0
    feed(p, now, {**HEALTHY, f"gear:{AP1}": None})
    w.ingest({"devices": [raw_ap(AP1, "Hall AP")], "clients": [raw_client("AA:00:00:00:00:10")]}, now)
    snap = w.snapshot(now)
    assert snap["gear"][0]["level"] == "bad" and snap["clients"][0]["diag"]["where"] == "ap"
    assert "Hall AP is the problem" in snap["clients"][0]["diag"]["title"]
    # A healthy Wi-Fi link behind a broken internet line: the device is told the line is at fault.
    w, p = tracker()
    feed(p, now, {**HEALTHY, "gateway": None, "inet:1.1.1.1": None, "inet:8.8.8.8": None})
    w.ingest({"devices": [raw_ap(AP1, "Hall AP")], "clients": [raw_client("AA:00:00:00:00:10")]}, now)
    d = w.snapshot(now)["clients"][0]["diag"]
    assert (d["level"], d["where"]) == ("bad", "isp") and "own Wi-Fi link is fine" in d["detail"]
    # Cable linked at 100 Mbps, and a mesh link, are both called out on the access point.
    w, p = tracker()
    feed(p, now, HEALTHY)
    w.ingest({"devices": [raw_ap(AP1, "Slow cable", uplink={"type": "wire", "speed": 100, "full_duplex": True}),
                          raw_ap(AP2, "Mesh", uplink={"type": "wireless", "signal": -70})], "clients": []}, now)
    issues = {g["name"]: " ".join(g["issues"]) for g in w.snapshot(now)["gear"]}
    assert "100 Mbps" in issues["Slow cable"] and "mesh" in issues["Mesh"]
    # An access point that lost its link is named, and its orphans' weak signal is explained by it.
    w, p = tracker()
    feed(p, now, HEALTHY)
    w.ingest({"devices": [raw_ap(AP1, "Hall AP"), raw_ap(AP2, "Garage", state=11, uplink={"type": "wireless"})],
              "clients": [raw_client("AA:00:00:00:00:10", signal=-81)]}, now)
    snap = w.snapshot(now)
    assert "isolated" in next(g for g in snap["gear"] if g["name"] == "Garage")["issues"][0] and snap["summary"]["apsDown"] == 1
    d = snap["clients"][0]["diag"]
    assert d["where"] == "signal" and "Garage is offline" in d["detail"] and d["fix"].startswith("Get Garage back")


def test_drops_and_roams_are_worked_out_from_readings_when_the_controller_sends_no_events(tmp_path):
    w, p = tracker(tmp_path)
    t = 2_000_000.0
    aps = [raw_ap(AP1, "Hall AP"), raw_ap(AP2, "Den AP")]
    dev = "AA:00:00:00:00:20"
    w.ingest({"devices": aps, "clients": [raw_client(dev)]}, t)
    assert not w.events                                         # the first reading is the baseline, not news
    w.ingest({"devices": aps, "clients": []}, t + 30)           # gone
    w.ingest({"devices": aps, "clients": [raw_client(dev, assoc=1060)]}, t + 60)    # and back: a drop
    w.ingest({"devices": aps, "clients": [raw_client(dev, ap=AP2, assoc=1090)]}, t + 90)    # moved to the other access point
    w.ingest({"devices": aps, "clients": [raw_client(dev, ap=AP2, assoc=1200)]}, t + 120)   # dropped and re-joined between readings
    assert [e["kind"] for e in w.events] == ["disconnected", "connected", "roamed", "reconnected"]
    assert w.counts(dev, t + 120) == {"drops1h": 2, "drops24h": 2, "roams1h": 1, "roams24h": 1, "lastDropS": 0}
    # Someone leaving for the afternoon is not a drop.
    w.ingest({"devices": aps, "clients": []}, t + 150)
    w.ingest({"devices": aps, "clients": [raw_client(dev, ap=AP2, assoc=9000)]}, t + 150 + 7200)
    assert w.counts(dev, t + 150 + 7200)["drops24h"] == 2
    # An access point going away and restarting is seen too, and everything is kept across a restart.
    w.ingest({"devices": [raw_ap(AP1, "Hall AP", state=0), raw_ap(AP2, "Den AP", uptime=40)], "clients": []}, t + 8000)
    assert {"gear-offline", "gear-restarted"} <= {e["kind"] for e in w.events}
    w.save(force=True)
    again, _ = tracker(tmp_path)
    assert len(again.events) == len(w.events)


def test_controller_events_are_used_once_and_replace_guessing():
    w, p = tracker()
    t = 2_000_000.0
    dev = "aa:00:00:00:00:30"
    ev = [{"_id": "e1", "key": "EVT_WU_Disconnected", "time": (t - 400) * 1000, "user": dev, "ap": AP1.lower(), "msg": f"User[{dev}] disconnected"},
          {"_id": "e2", "key": "EVT_WU_Connected", "time": (t - 380) * 1000, "user": dev, "ap": AP1.lower(), "msg": f"User[{dev}] has connected to AP[{AP1.lower()}]"},
          {"_id": "e3", "key": "EVT_AP_Lost_Contact", "time": (t - 200) * 1000, "ap": AP1.lower(), "msg": f"AP[{AP1.lower()}] was disconnected"},
          {"_id": "e4", "key": "EVT_LU_Connected", "time": (t - 100) * 1000, "user": "aa:00:00:00:00:99", "msg": "wired"}]
    raw = {"devices": [raw_ap(AP1, "Hall AP")], "clients": [raw_client(dev)], "events": ev}
    w.ingest(raw, t)
    w.ingest(raw, t + 30)                                       # the same events again: not counted twice
    assert [e["kind"] for e in w.events] == ["disconnected", "connected", "gear-offline"]
    assert w.events[1]["detail"] == "dev-30 has connected to Hall AP" and w.controller_events
    assert w.counts(dev.upper(), t + 30)["drops24h"] == 1
    w.ingest({**raw, "clients": []}, t + 60)                    # with real events, a missing client is not guessed at
    assert len(w.events) == 3


def test_watching_a_device_pings_it_fast_even_when_it_is_off_the_wifi(tmp_path):
    w, p = tracker(tmp_path)
    t = 2_000_000.0
    dev = "AA:00:00:00:00:40"
    w.ingest({"devices": [raw_ap(AP1, "Hall AP")], "clients": [raw_client(dev)]}, t)
    assert w.targets()[f"sta:{dev}"] == ("10.20.0.64", "slow") and w.targets()[f"gear:{AP1}"][1] == "fast"
    w.set_watch(dev, True)
    assert w.targets()[f"sta:{dev}"] == ("10.20.0.64", "fast")
    w.ingest({"devices": [raw_ap(AP1, "Hall AP")], "clients": []}, t + 30)
    assert w.targets()[f"sta:{dev}"] == ("10.20.0.64", "fast")
    row = w.snapshot(t + 30)["clients"][0]
    assert not row["online"] and row["watched"] and row["diag"]["where"] == "offline"
    again, _ = tracker(tmp_path)
    assert dev in again.watch


# ---------------------------------------------------------------------------------------------- the map and the API
def test_wifi_on_the_map_and_in_triage(state):
    g = settle(state)
    ipad = next(n for n in g["nodes"] if n.get("device", {}).get("mac") == IPAD)
    assert ipad["name"] == "Sam's iPad"                         # the name given in the Wi-Fi controller wins
    assert ipad["device"]["wifi"]["ap"] == "Living room AP" and ipad["device"]["wifi"]["level"] == "ok"
    wifi = [f for f in g["findings"] if f["category"] == "wifi"]
    bell = next(f for f in wifi if "Ring doorbell" in f["title"])
    assert bell["severity"] == "warning" and "weak signal" in bell["title"] and bell["subject"].startswith("wl:smart-home/")
    assert any("78% busy" in f["title"] and f["pattern"] == "wifi:gear" for f in wifi)
    assert any("mesh" in f["title"] + f["detail"] for f in wifi)      # the sample access point also loses a ping now and then
    assert not any("Garage AP has no cable" in f["title"] for f in wifi)        # said once, on the access point, not per device
    assert g["network"]["wifi"]["aps"] == 2 and g["network"]["wifi"]["bad"] == 1 and g["network"]["wifi"]["level"] == "ok"
    assert g["sources"]["wifi"]["enabled"] and g["sources"]["wifi"]["clients"] == 8
    assert not any(f["id"] in ("platform:unifi", "platform:pings") for f in g["findings"])


def test_wifi_api(settings):
    import importlib
    import netgalaxy.main as main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    act = {"X-Requested-With": "network-galaxy"}
    with TestClient(main.app) as c:
        main.state.refresh_once()                               # do not race the background loop for the first reading
        w = c.get("/api/v1/wifi").json()
        assert w["summary"]["clients"] == 8 and [h["id"] for h in w["upstream"]["hops"]] == ["router", "internet", "dns"]
        assert w["clients"][0]["mac"] == BELL and len(w["upstream"]["hops"][0]["spark"]) >= 59
        d = c.get(f"/api/v1/wifi/device?mac={BELL}").json()
        assert d["device"]["diag"]["where"] == "signal" and d["ap"]["name"] == "Garage AP"
        assert [p["name"] for p in d["path"]] == ["Internet", "Router", "Garage AP", "Ring doorbell"]
        assert d["history"]["hour"] and d["events"] and d["signal"]
        assert c.get("/api/v1/wifi/device?mac=00:00:00:00:00:00").status_code == 404
        assert c.get("/api/v1/wifi/device?mac=nonsense").status_code == 404
        assert c.post("/api/v1/wifi/watch", json={"mac": BELL}).status_code == 403                 # no action header
        assert c.post("/api/v1/wifi/watch", json={"mac": "zz"}, headers=act).status_code == 400
        assert c.post("/api/v1/wifi/watch", json={"mac": BELL}, headers=act).json()["watching"] == [BELL]
        assert next(x for x in c.get("/api/v1/wifi").json()["clients"] if x["mac"] == BELL)["watched"]
        assert c.post("/api/v1/wifi/watch", json={"mac": BELL, "on": False}, headers=act).json()["watching"] == []
        t = c.post("/api/v1/wifi/test", json={"mac": BELL}, headers=act).json()
        assert [h["name"] for h in t["hops"]] == ["Internet", "Router", "Garage AP", "Ring doorbell"] and t["hops"][0]["sent"] == 12
        assert c.post("/api/v1/wifi/test", json={"mac": BELL}).status_code == 403
        assert "wifi" in [x["id"] for x in c.get("/api/v1/meta").json()["categories"]]
