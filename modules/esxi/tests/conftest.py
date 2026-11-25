import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "collector"))
sys.path.insert(0, str(ROOT.parents[1] / "core" / "py"))


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for k in ("ESXIGALAXY_HOST", "ESXIGALAXY_USER", "ESXIGALAXY_PASSWORD", "ESXIGALAXY_SITE_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ESXIGALAXY_MODE", "sample")
    monkeypatch.setenv("ESXIGALAXY_DATA_DIR", str(tmp_path / "data"))
    from esxigalaxy.config import load_settings
    return load_settings()


@pytest.fixture
def state(settings):
    from esxigalaxy.state import EsxiState
    return EsxiState(settings)


@pytest.fixture
def graph(state):
    state.refresh_once()
    return state.refresh_once()          # the second reading has peaks to size from


@pytest.fixture
def site_file(tmp_path):
    def write(data: dict) -> str:
        path = tmp_path / "site.json"
        path.write_text(json.dumps(data))
        return str(path)
    return write
