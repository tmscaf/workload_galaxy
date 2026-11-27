# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Blocking: what may be blocked, the action login, and the list the router pulls."""
import importlib
import time

import pytest
from conftest import settle

from netgalaxy.auth import Auth
from netgalaxy.blocks import BlockError, BlockList, normalise

PROTECT = ["192.168.1.0/24", "192.168.1.1", "198.51.100.7"]


@pytest.mark.parametrize("target,why", [
    ("192.168.1.20", "protected"), ("10.0.0.5", "private"), ("198.51.100.7", "protected"),
    ("8.8.0.0/16", "too wide"), ("0.0.0.0/0", "too wide"), ("not-an-ip", "not an IP"), ("2001:db8::1", "IPv4"),
    ("203.0.113.9", "private or reserved"),                   # documentation range: never a real target
])
def test_refuses_what_must_never_be_blocked(target, why):
    with pytest.raises(BlockError, match=why):
        normalise(target, protected=PROTECT)


def test_accepts_internet_addresses_and_small_ranges():
    assert normalise(" 45.155.205.233 ", protected=PROTECT) == "45.155.205.233"
    assert normalise("45.155.205.0/24", protected=PROTECT) == "45.155.205.0/24"
    assert normalise("45.155.205.7/24", protected=PROTECT) == "45.155.205.0/24"
    assert normalise("45.155.205.7/32", protected=PROTECT) == "45.155.205.7"


def test_entries_expire_and_persist(tmp_path):
    bl = BlockList(str(tmp_path), PROTECT)
    bl.add("45.155.205.233", reason="scanner", by="admin", expires="1d", now=1000.0)
    bl.add("94.183.174.0/24", reason="threat list", by="admin", expires="never", now=1000.0)
    body, digest = bl.text(now=2000.0)
    assert body == "45.155.205.233\n94.183.174.0/24\n" and len(digest) == 16
    again = BlockList(str(tmp_path), PROTECT)
    assert set(again.entries) == {"45.155.205.233", "94.183.174.0/24"}
    assert again.text(now=1000.0 + 86401)[0] == "94.183.174.0/24\n"           # the 1-day block expired
    assert again.history[-1]["how"] == "expired"


def test_router_reports_what_it_applied(tmp_path):
    bl = BlockList(str(tmp_path), PROTECT)
    bl.add("45.155.205.233", reason="", by="admin", now=1000.0)
    _body, digest = bl.text(now=1001.0)
    bl.router_fetch(have=None, rejected=None, sender="192.168.1.1", now=1002.0)
    assert not bl.snapshot(now=1003.0)["inSync"] and bl.entries["45.155.205.233"]["appliedAt"] is None
    bl.router_fetch(have=digest, rejected=0, sender="192.168.1.1", now=1060.0)
    snap = bl.snapshot(now=1061.0)
    assert snap["inSync"] and snap["entries"][0]["appliedAt"] == 1060.0 and snap["router"]["rejected"] == 0


def test_login_locks_out_after_repeated_failures():
    a = Auth("correct horse")
    for _ in range(5):
        assert a.login("10.0.0.9", "wrong", now=100.0) is None
    assert a.locked("10.0.0.9", now=101.0) > 0
    assert a.login("10.0.0.9", "correct horse", now=101.0) is None             # locked even with the right one
    token = a.login("10.0.0.8", "correct horse", now=101.0)                    # another client is fine
    assert a.valid(token, now=102.0) and not a.valid(token, now=101.0 + 13 * 3600) and not a.valid(token + "x", now=102.0)
    assert not Auth("").enabled and Auth("").login("x", "") is None


def test_findings_offer_block_and_show_blocked(state):
    settle(state)
    g = state.refresh_once()
    threat = next(f for f in g["findings"] if f["category"] == "threat")
    assert threat["block"] == [{"target": "203.0.113.66", "blocked": None}]
    state.blocks.add("203.0.113.66", reason="threat", by="admin")              # sample mode allows documentation ranges
    g = state.refresh_once()
    threat = next(f for f in g["findings"] if f["category"] == "threat")
    assert threat["block"][0]["blocked"]["target"] == "203.0.113.66"
    ext = next(n for n in g["nodes"] if n["id"] == "ext:ip:203.0.113.66")
    assert ext["ext"]["allBlocked"] and ext["ext"]["blockTargets"] == ["203.0.113.66"]
    netflix = next(n for n in g["nodes"] if n["id"] == "ext:app:netflix")
    assert netflix["ext"]["blockTargets"] == []                                # a named service is never offered


def _client(monkeypatch, **env):
    from fastapi.testclient import TestClient
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import netgalaxy.main as main
    importlib.reload(main)
    return main, TestClient(main.app)


def test_api_actions_need_a_session(settings, monkeypatch):
    main, client = _client(monkeypatch, NETGALAXY_ADMIN_PASSWORD="s3cret pass", NETGALAXY_BLOCKLIST_TOKEN="tok123")
    H = {"X-Requested-With": "network-galaxy"}
    with client:
        assert client.get("/api/v1/session").json() == {"signedIn": False, "actions": True, "routerSync": True, "lockedFor": 0}
        assert client.post("/api/v1/blocks", json={"target": "203.0.113.66"}, headers=H).status_code == 401
        assert client.post("/api/v1/login", json={"password": "nope"}, headers=H).status_code == 401
        assert client.post("/api/v1/login", json={"password": "s3cret pass"}).status_code == 403    # no action header
        assert client.post("/api/v1/login", json={"password": "s3cret pass"}, headers=H).status_code == 200
        assert client.post("/api/v1/blocks", json={"target": "203.0.113.66"}).status_code == 403      # no action header
        r = client.post("/api/v1/blocks", json={"target": "203.0.113.66", "reason": "test", "expires": "1d"}, headers=H)
        assert r.status_code == 200 and r.json()["count"] == 1
        bad = client.post("/api/v1/blocks", json={"target": "10.20.0.30"}, headers=H)
        assert bad.status_code == 400 and "protected" in bad.json()["detail"]
        # The router fetch: token and sender both checked (the test client's address is "testclient").
        assert client.get("/api/v1/blocklist?token=tok123").status_code == 403
        main.settings.blocklist_senders = ["testclient"]
        assert client.get("/api/v1/blocklist?token=wrong").status_code == 403
        lst = client.get("/api/v1/blocklist?token=tok123")
        assert lst.text == "203.0.113.66\n" and lst.headers["X-Blocklist-Hash"]
        client.get(f"/api/v1/blocklist?token=tok123&have={lst.headers['X-Blocklist-Hash']}&rejected=0")
        assert client.get("/api/v1/blocks").json()["inSync"] is True
        assert client.delete("/api/v1/blocks/203.0.113.66", headers=H).status_code == 200
        assert client.get("/api/v1/blocks").json()["count"] == 0
        client.post("/api/v1/logout", headers=H)
        assert client.delete("/api/v1/blocks/203.0.113.66", headers=H).status_code == 401


def test_api_actions_off_without_a_password(settings, monkeypatch):
    monkeypatch.delenv("NETGALAXY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("NETGALAXY_BLOCKLIST_TOKEN", raising=False)
    _main, client = _client(monkeypatch)
    with client:
        assert client.post("/api/v1/login", json={"password": "x"}, headers={"X-Requested-With": "network-galaxy"}).status_code == 403
        assert client.get("/api/v1/blocklist?token=").status_code == 404
