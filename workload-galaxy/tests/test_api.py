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
