"""The suite shell: which modules it lists, how it reports their state, and what it serves."""
import urllib.error

from fastapi.testclient import TestClient


def test_modules_are_parsed_in_order_and_bad_entries_dropped():
    from galaxyshell.config import load_settings, parse_modules
    mods = parse_modules(["kubernetes=http://a:8090", "esxi=https://b/esxi/", "Bad Id=http://x", "ftp=ftp://x", "nourl",
                          "kubernetes=http://dup", "backup-jobs=http://c"])
    assert [m["id"] for m in mods] == ["kubernetes", "esxi", "backup-jobs"]
    assert mods[0]["title"] == "Kubernetes" and mods[0]["url"] == "http://a:8090/" and mods[0]["probeUrl"] == "http://a:8090/"
    assert mods[1]["url"] == "https://b/esxi/" and mods[2]["title"] == "Backup Jobs"
    # a file entry can carry its own title and a separate address for the shell to check it on
    extra = parse_modules([], [{"id": "network", "title": "Home network", "url": "http://lan:8091", "probeUrl": "http://svc.ns:80"}])
    assert extra[0]["title"] == "Home network" and extra[0]["probeUrl"] == "http://svc.ns:80/"
    s = load_settings({"modules": "network=http://n,esxi=http://e"}, environ={})
    assert [m["id"] for m in s.module_list] == ["network", "esxi"] and s.probe_seconds == 20


def test_a_module_reports_its_state(monkeypatch):
    from galaxyshell import probe
    answers = {
        "http://up/api/v1/meta": {"version": "1.2.0", "mode": "sample", "cluster": {"name": "lab"}, "error": None},
        "http://up/api/v1/triage?severity=critical": {"summary": {"problems": 5, "problemsCritical": 2}},
        "http://calm/api/v1/meta": {"version": "0.1.0", "mode": "live", "estate": {"name": "esx-01"}, "error": "host timed out"},
        "http://calm/api/v1/triage?severity=critical": {"summary": {"problems": 0, "problemsCritical": 0}},
        "http://warming/api/v1/meta": {"version": "0.3.0", "mode": "live"},
    }

    def fake(url, verify):
        if url.startswith("http://down/"):
            raise urllib.error.URLError(ConnectionRefusedError(61, "Connection refused"))
        if url not in answers:
            raise urllib.error.HTTPError(url, 503, "loading", None, None)
        return answers[url]
    monkeypatch.setattr(probe, "_get", fake)
    mod = lambda host: {"id": host, "probeUrl": f"http://{host}/"}      # noqa: E731
    up = probe.check(mod("up"))
    assert (up["ok"], up["version"], up["name"], up["problems"], up["critical"], up["level"]) == (True, "1.2.0", "lab", 5, 2, "critical")
    calm = probe.check(mod("calm"))
    assert (calm["level"], calm["name"], calm["stale"]) == ("ok", "esx-01", "host timed out")
    warming = probe.check(mod("warming"))                       # answering, but its first reading is not in yet
    assert warming["ok"] is True and warming["problems"] is None and warming["level"] == "ok"
    down = probe.check(mod("down"))
    assert down["ok"] is False and down["error"] == "connection refused" and down["level"] == "off"
    assert probe.check(mod("missing"))["error"] == "answered 503"


def test_api_lists_modules_with_state_and_serves_the_page():
    from galaxyshell.config import load_settings
    from galaxyshell.main import create_app
    from galaxyshell.probe import Prober
    settings = load_settings({"modules": "kubernetes=http://k:8090,esxi=http://e:8092"}, environ={})
    states = {"kubernetes": {"ok": True, "error": None, "version": "0.11.0", "mode": "live", "name": "prod", "problems": 3,
                             "critical": 0, "level": "warning", "stale": None},
              "esxi": {"ok": False, "error": "connection refused", "version": "", "mode": "", "name": "", "problems": None,
                       "critical": None, "level": "off", "stale": None}}
    prober = Prober(settings.module_list, seconds=3600, checker=lambda m, verify: states[m["id"]])
    app = create_app(settings, prober)
    with TestClient(app) as c:
        blank = c.get("/api/v1/modules").json()            # before or after the first check: every module is listed either way
        assert [m["id"] for m in blank["modules"]] == ["kubernetes", "esxi"]
        prober.refresh()
        mods = {m["id"]: m for m in c.get("/api/v1/modules").json()["modules"]}
        assert mods["kubernetes"]["problems"] == 3 and mods["kubernetes"]["url"] == "http://k:8090/" and mods["kubernetes"]["title"] == "Kubernetes"
        assert mods["esxi"]["ok"] is False and mods["esxi"]["error"] == "connection refused" and mods["esxi"]["checkedS"] is not None
        assert "probeUrl" not in mods["kubernetes"]         # where the shell checks a module is not the browser's business
        page = c.get("/")
        assert page.status_code == 200 and "Workload Galaxy" in page.text and "/js/shell.js" in page.text
        assert page.headers["cache-control"] == "no-store"
        asset = page.text.split('src="')[1].split('"')[0]
        assert c.get("/" + asset).status_code == 200
        assert c.get("/v/x/../../app/galaxyshell/config.py").status_code == 404
        assert c.get("/healthz").json() == {"ok": True}


def test_no_modules_configured_is_not_an_error():
    from galaxyshell.config import load_settings
    from galaxyshell.main import create_app
    with TestClient(create_app(load_settings({}, environ={}))) as c:
        assert c.get("/api/v1/modules").json()["modules"] == [] and c.get("/").status_code == 200
