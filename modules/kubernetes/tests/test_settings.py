# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Settings from the suite: the form, the token, and saving (which restarts the collector in place)."""
import importlib
import json
import os

import pytest
from fastapi.testclient import TestClient

AUTH = {"Authorization": "Bearer tok-456"}


@pytest.fixture
def main(monkeypatch):
    from galaxy_core import settingsapi
    from galaxy import main as m
    before = dict(os.environ)             # loading the module lays saved settings over the environment: put it back after
    restarts = []
    monkeypatch.setattr(settingsapi, "restart_in_place", lambda env, before=None: restarts.append(env))
    monkeypatch.setenv("GALAXY_SETTINGS_TOKEN", "tok-456")
    importlib.reload(m)
    m.restarts = restarts
    yield m
    monkeypatch.undo()
    os.environ.clear()
    os.environ.update(before)
    importlib.reload(m)                                       # leave the module as the other tests expect it


def test_form_lists_what_can_be_changed_and_hides_the_agent_token(main):
    with TestClient(main.app) as c:
        assert c.get("/api/v1/settings").status_code == 401
        form = c.get("/api/v1/settings", headers=AUTH).json()
        by = {f["name"]: f for f in form["fields"]}
        assert form["writable"] and form["appliesBy"] == "restart" and form["persistent"] is False and form["missing"] == []
        assert by["mode"]["value"] == "demo" and by["refresh_seconds"]["value"] == 15 and by["hide_self"]["value"] is True
        assert "3306" in by["probe_skip_ports"]["value"]
        assert "agent_token" not in by and "test-token" not in json.dumps(form)
        assert c.get("/api/v1/module").json()["id"] == "kubernetes"


def test_saving_restarts_and_the_next_run_uses_what_was_saved(main, monkeypatch):
    with TestClient(main.app) as c:
        assert c.put("/api/v1/settings", headers=AUTH, json={"values": {"refresh_seconds": "fast"}}).status_code == 400
        assert c.put("/api/v1/settings", headers=AUTH, json={"values": {"agent_token": "mine"}}).status_code == 400
        saved = c.put("/api/v1/settings", headers=AUTH, json={"values": {"exclude_namespaces": "data, observability", "refresh_seconds": 30}}).json()
        assert saved["saved"] and saved["restarting"] and len(main.restarts) == 1
        assert c.post("/api/v1/settings/test", headers=AUTH, json={"values": {}}).json()["ok"] is True
    env = main.restarts[0]
    assert json.loads(env["GALAXY__SAVED_SETTINGS"]) == {"exclude_namespaces": ["data", "observability"], "refresh_seconds": 30}
    monkeypatch.setenv("GALAXY__SAVED_SETTINGS", env["GALAXY__SAVED_SETTINGS"])     # the restart hands them over like this
    importlib.reload(main)
    assert main.settings.exclude_namespaces == ["data", "observability"] and main.settings.refresh_seconds == 30
    with TestClient(main.app) as c:
        main.state.refresh_once()
        names = {n["name"] for n in c.get("/api/v1/namespaces").json()["namespaces"]}
        assert "storefront" in names and not {"data", "observability"} & names


def test_without_a_token_nothing_is_revealed_or_changed(monkeypatch):
    from galaxy import main as m
    assert "GALAXY_EXCLUDE_NAMESPACES" not in os.environ      # nothing left behind by the tests above
    monkeypatch.delenv("GALAXY_SETTINGS_TOKEN", raising=False)
    importlib.reload(m)
    with TestClient(m.app) as c:
        form = c.get("/api/v1/settings").json()
        assert form["writable"] is False and all(f["value"] is None for f in form["fields"])
        assert c.put("/api/v1/settings", json={"values": {"refresh_seconds": 30}}).status_code == 403
