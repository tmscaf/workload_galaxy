# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Plugins: a module's optional add-ons, their switches, and what an add-on may and may not do."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from galaxy_core.plugins import Plugin, Plugins
from galaxy_core.settings import Field


class Greeter(Plugin):
    id = "greeter"
    title = "Greeter"
    summary = "Says hello."
    tab = {"title": "Greeter", "hint": "Hello"}
    fields = [Field("greeter_word", default="hello", label="Word"), Field("greeter_times", kind="int", default=1, minimum=1)]
    started = stopped = 0

    def start(self):
        Greeter.started += 1

    def stop(self):
        Greeter.stopped += 1

    def routes(self, router, ctx):
        @router.get("/say")
        def say():
            return {"said": " ".join([self.settings.word] * self.settings.times), "mode": ctx.mode}

    def snapshot(self):
        return {"word": self.settings.word}

    def source(self):
        return {"enabled": True}

    def diagnose(self, d):
        d.append(f"{self.settings.word} from the greeter")


class Broken(Plugin):
    id = "broken"
    title = "Broken"
    summary = "Fails at everything."

    def start(self):
        raise RuntimeError("no")

    def snapshot(self):
        raise RuntimeError("no")

    def diagnose(self, d):
        raise RuntimeError("no")


CTX = SimpleNamespace(mode="sample")


def test_every_plugin_gets_a_switch_and_its_settings_under_one_heading():
    fields = Plugins([Greeter], "X").fields()
    assert [f.name for f in fields] == ["greeter_enabled", "greeter_word", "greeter_times"]
    assert fields[0].kind == "bool" and fields[0].default is False              # off until someone turns it on
    assert {f.group for f in fields} == {"Plugin: Greeter"}
    assert Greeter.fields[0].group == ""                                        # the class's own list is left alone


def test_a_plugin_is_off_until_switched_on():
    off = Plugins([Greeter], "X").load(CTX, environ={})
    assert off.active == [] and off.describe() == [
        {"id": "greeter", "title": "Greeter", "summary": "Says hello.", "enabled": False, "tab": None}]
    app = FastAPI()
    off.mount(app)
    with TestClient(app) as c:
        assert c.get("/api/v1/plugins").json()["plugins"][0]["enabled"] is False
        assert c.get("/api/v1/plugins/greeter").status_code == 404              # no page, no routes
        assert c.get("/api/v1/plugins/greeter/say").status_code == 404


def test_a_plugin_that_is_on_gets_its_settings_page_routes_and_findings():
    on = Plugins([Greeter], "X").load(CTX, environ={"X_GREETER_ENABLED": "true", "X_GREETER_WORD": "hi", "X_GREETER_TIMES": "2"})
    assert on.get("greeter").settings.word == "hi" and on.describe()[0]["tab"] == {"title": "Greeter", "hint": "Hello"}
    before = (Greeter.started, Greeter.stopped)
    on.start()
    on.stop()
    assert (Greeter.started, Greeter.stopped) == (before[0] + 1, before[1] + 1)
    findings = []
    on.diagnose(findings)
    assert findings == ["hi from the greeter"] and on.sources() == {"greeter": {"enabled": True}}
    app = FastAPI()
    on.mount(app)
    with TestClient(app) as c:
        assert c.get("/api/v1/plugins/greeter").json() == {"id": "greeter", "title": "Greeter", "word": "hi"}
        assert c.get("/api/v1/plugins/greeter/say").json() == {"said": "hi hi", "mode": "sample"}


def test_a_failing_plugin_never_takes_its_module_down():
    both = Plugins([Broken, Greeter], "X").load(CTX, environ={"X_BROKEN_ENABLED": "1", "X_GREETER_ENABLED": "1"})
    both.start()                                                                # Broken raises; Greeter still starts
    findings = []
    both.diagnose(findings)
    assert findings == ["hello from the greeter"] and both.sources()["broken"] == {}
    app = FastAPI()
    both.mount(app)
    with TestClient(app) as c:
        assert c.get("/api/v1/plugins/broken").status_code == 500
        assert c.get("/api/v1/plugins/greeter").status_code == 200


def test_plugin_settings_must_carry_the_plugins_name():
    class Stray(Plugin):
        id = "stray"
        title = "Stray"
        fields = [Field("host")]                        # would collide with the module's own "host"

    class Switch(Plugin):
        id = "switch"
        title = "Switch"
        fields = [Field("switch_enabled", kind="bool")]  # the switch is the frame's, not the plugin's

    for cls in (Stray, Switch):
        with pytest.raises(ValueError, match="must be named"):
            Plugins([cls], "X")
    with pytest.raises(ValueError, match="unique"):
        Plugins([Greeter, Greeter], "X")
