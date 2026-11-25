"""Settings from the suite: the form, the token, saving (which restarts the module) and the saved site details."""
import importlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer tok-123"}


@pytest.fixture
def main(settings, tmp_path, monkeypatch):
    from galaxy_core import settingsapi
    before = dict(os.environ)             # loading the module lays saved settings over the environment: put it back after
    yield from _main(settings, tmp_path, monkeypatch, settingsapi)
    os.environ.clear()
    os.environ.update(before)


def _main(settings, tmp_path, monkeypatch, settingsapi):
    restarts = []
    monkeypatch.setattr(settingsapi, "restart_in_place", lambda env, before=None: restarts.append(env))
    monkeypatch.setenv("NETGALAXY_SETTINGS_TOKEN", "tok-123")
    monkeypatch.setenv("NETGALAXY_NTOPNG_URL", "https://router.example:3000")
    monkeypatch.setenv("NETGALAXY_NTOPNG_USER", "viewer")
    monkeypatch.setenv("NETGALAXY_NTOPNG_PASSWORD", "deployed-secret")
    import netgalaxy.main as m
    importlib.reload(m)
    m.restarts = restarts
    yield m


def test_settings_form_needs_the_token_and_never_shows_a_password(main):
    with TestClient(main.app) as c:
        assert c.get("/api/v1/settings").status_code == 401
        form = c.get("/api/v1/settings", headers=AUTH).json()
        by = {f["name"]: f for f in form["fields"]}
        assert form["writable"] and form["persistent"] and form["appliesBy"] == "restart" and form["mode"] == "sample"
        assert by["ntopng_url"]["value"] == "https://router.example:3000" and by["ntopng_url"]["source"] == "environment"
        assert by["ntopng_password"]["value"] is None and by["ntopng_password"]["set"] is True
        assert "deployed-secret" not in json.dumps(form)
        assert by["lan_cidrs"]["value"] == ["10.20.0.0/24"] and by["router_ip"]["value"] == "10.20.0.1"
        assert not {"settings_token", "data_dir", "syslog_port", "config_file", "web_root"} & set(by)
        assert c.get("/api/v1/module").json()["id"] == "network"


def test_saving_writes_the_file_and_restarts_and_a_new_address_forgets_the_password(main, tmp_path):
    with TestClient(main.app) as c:
        bad = c.put("/api/v1/settings", headers=AUTH, json={"values": {"site": "{not json"}})
        assert bad.status_code == 400 and "not valid JSON" in bad.json()["detail"] and main.restarts == []
        assert c.put("/api/v1/settings", headers=AUTH, json={"values": {"site": "[1, 2]"}}).status_code == 400
        saved = c.put("/api/v1/settings", headers=AUTH, json={"values": {
            "network_name": "Office", "ntopng_url": "https://other.example:3000",
            "site": json.dumps({"forwards": [{"name": "Game", "port": "27015", "proto": "udp", "target": "10.20.0.11"}]})}}).json()
        assert saved["saved"] and saved["restarting"] and saved["forgot"] == ["ntopng password"]
        assert len(main.restarts) == 1 and main.restarts[0]["NETGALAXY_NTOPNG_URL"] == "https://router.example:3000"   # the deployment's own
    on_disk = json.loads((tmp_path / "data" / "settings.json").read_text())
    assert on_disk["network_name"] == "Office" and on_disk["ntopng_password"] == "" and "deployed-secret" not in json.dumps(on_disk)


def test_after_the_restart_the_saved_settings_and_site_details_are_in_use(main, tmp_path, monkeypatch):
    with TestClient(main.app) as c:
        c.put("/api/v1/settings", headers=AUTH, json={"values": {
            "network_name": "Office", "site": json.dumps({"devices": [{"ip": "10.20.0.12", "name": "front-desk-pc", "type": "computer"}]})}})
    importlib.reload(main)                                   # what the restart does: the module loads again
    assert main.settings.network_name == "Office" and main.settings.site["devices"][0]["name"] == "front-desk-pc"
    assert main.settings.ntopng_password == "deployed-secret"                 # untouched, so still the deployment's
    with TestClient(main.app) as c:
        for _ in range(100):
            if c.get("/readyz").status_code == 200:
                break
            time.sleep(0.05)
        assert c.get("/api/v1/meta").json()["network"]["name"] == "Office"
        assert any(n["name"] == "front-desk-pc" for n in c.get("/api/v1/topology").json()["nodes"])
        assert {f["name"]: f for f in c.get("/api/v1/settings", headers=AUTH).json()["fields"]}["network_name"]["source"] == "saved"


def test_test_connection_says_what_is_missing_without_saving(main):
    with TestClient(main.app) as c:
        assert c.post("/api/v1/settings/test", headers=AUTH, json={"values": {}}).json()["ok"] is True          # sample data
        r = c.post("/api/v1/settings/test", headers=AUTH, json={"values": {"mode": "live", "ntopng_url": "https://new.example:3000"}}).json()
        assert r["ok"] is False and "ntopng password" in r["message"] and main.restarts == []
