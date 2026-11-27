# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""HTTP surface: what's running is reported consistently, and the UI is never served stale."""
import os
import pathlib

os.environ["GALAXY_MODE"] = "demo"
os.environ["GALAXY_AGENT_TOKEN"] = "test-token"

from fastapi.testclient import TestClient  # noqa: E402

from galaxy import __version__, main  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_version_is_single_sourced_and_agent_mismatch_is_reported():
    assert __version__ == (ROOT / "VERSION").read_text().strip()
    with TestClient(main.app) as c:
        main.state.refresh_once()
        meta = c.get("/api/v1/meta").json()
        assert meta["version"] == __version__ and meta["build"] and meta["startedAt"]
        assert c.get("/api/v1/version").json()["agentsMismatched"] == []
        r = c.post("/api/v1/agent/report", json={"node": "old-node", "agentVersion": "0.1.0", "netns": []},
                   headers={"Authorization": "Bearer test-token"})
        assert r.status_code == 202
        v = c.get("/api/v1/version").json()
        assert "old-node" in v["agentsMismatched"]
        assert {"node": "old-node", "version": "0.1.0"}.items() <= next(a for a in v["agents"] if a["node"] == "old-node").items()


def test_app_files_revalidate_but_vendor_libraries_may_cache():
    with TestClient(main.app) as c:
        assert c.get("/js/main.js").headers["cache-control"] == "no-cache"
        assert c.get("/").headers["cache-control"] == "no-store"
        assert "no-cache" not in c.get("/vendor/three.module.min.js").headers.get("cache-control", "")


def test_page_loads_code_from_build_versioned_urls():
    with TestClient(main.app) as c:
        html = c.get("/").text
        bid = main.build_id()
        assert f'src="v/{bid}/js/main.js"' in html and f'href="v/{bid}/css/app.css"' in html
        assert f'name="galaxy-build" content="{bid}"' in html
        r = c.get(f"/v/{bid}/js/scene.js")
        assert r.status_code == 200 and "javascript" in r.headers["content-type"] and "disposeTree" in r.text
        # local builds revalidate; stamped builds are immutable (checked via the header logic)
        assert r.headers["cache-control"] in ("no-cache", "public, max-age=31536000, immutable")
        assert c.get("/v/some-old-build/js/main.js").headers["cache-control"] == "no-cache"
        assert c.get(f"/v/{bid}/../../collector/galaxy/config.py").status_code == 404
        assert c.get(f"/v/{bid}/%2e%2e/%2e%2e/VERSION").status_code == 404


def test_triage_endpoint_serves_the_dashboard_as_data_and_logs_are_scoped():
    with TestClient(main.app) as c:
        main.state.source.set_scenarios(["bad-rollout", "quota-exceeded"])
        main.state.refresh_once()
        t = c.get("/api/v1/triage").json()
        assert t["counts"]["critical"] >= 3 and t["rootCauses"] >= 3
        assert any(f["category"] == "rollout" for f in t["findings"]) and t["platform"]["nodes"]["total"] == 3
        only = c.get("/api/v1/triage?ns=payments&severity=critical").json()
        assert only["findings"] and all(f["severity"] == "critical" for f in only["findings"])
        assert {p["name"] for p in only["projects"]} == {"payments"}
        assert all(f["namespace"] in ("payments", "") for f in only["findings"])
        topo = c.get("/api/v1/topology").json()
        assert all("template" not in n and "labels" not in n and "diag" not in n for n in topo["nodes"])
        search = next(n for n in topo["nodes"] if n["id"] == "wl:storefront/Deployment/search")
        crashing = next(p for p in search["pods"] if not p["ready"])
        lines = c.get(f"/api/v1/logs?ns=storefront&pod={crashing['name']}&previous=true").json()["lines"]
        assert any("FATAL" in x for x in lines)
        main.state.source.set_scenarios([])
        main.state.refresh_once()


def test_real_cluster_logs_are_off_unless_enabled(monkeypatch):
    monkeypatch.setattr(main.settings, "mode", "cluster")
    monkeypatch.setattr(main.settings, "pod_logs", False)
    with TestClient(main.app) as c:
        assert c.get("/api/v1/logs?ns=storefront&pod=x").status_code == 403


def test_showcase_presets_only_name_real_scenarios():
    from galaxy.scenarios import PRESETS, SCENARIOS
    assert len(PRESETS) >= 4
    for p in PRESETS:
        assert p["title"] and p["summary"] and set(p["scenarios"]) <= set(SCENARIOS)
    with TestClient(main.app) as c:
        assert [p["id"] for p in c.get("/api/v1/demo/scenarios").json()["presets"]] == [p["id"] for p in PRESETS]


def test_scale_endpoints_serve_a_small_cluster_too():
    with TestClient(main.app) as c:
        main.state.refresh_once()
        topo = c.get("/api/v1/topology").json()
        assert topo["view"]["level"] == "workloads" and "_scoped" not in topo and topo["summary"]["problems"] >= 4
        assert c.get("/api/v1/topology", headers={"Accept-Encoding": "gzip"}).headers.get("content-encoding") == "gzip"
        rows = c.get("/api/v1/workloads?problems=true&sort=name&desc=false").json()
        assert rows["total"] >= 3 and rows["rows"][0]["name"] <= rows["rows"][-1]["name"]
        assert c.get("/api/v1/calls?limit=5").json()["rows"]
        wid = "wl:payments/Deployment/fraud-check"
        d = c.get(f"/api/v1/node?id={wid}").json()
        assert d["at"] == wid and d["nodes"][0]["id"] == wid and d["edges"]
        assert c.get("/api/v1/node?id=wl:none/Deployment/x").status_code == 404
        assert c.get("/api/v1/search?q=fraud").json()["hits"][0]["name"] == "fraud-check"
