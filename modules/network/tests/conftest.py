import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "core", "py"))

SITE = {
    "publicAddress": "192.0.2.1",
    "forwards": [{"name": "Game server", "port": "27015", "proto": "udp", "target": "10.20.0.11"},
                 {"name": "Old web box", "port": "8088", "proto": "tcp", "target": "10.20.0.77", "targetPort": "80"},
                 {"name": "NAS SSH", "port": "22", "proto": "tcp", "target": "10.20.0.10"}],
    "hosts": [{"id": "hypervisor", "name": "Hypervisor", "ouis": ["00:0C:29"]}],
    "devices": [{"mac": "00:0C:29:AA:00:12", "name": "web-vm", "type": "server"}],
}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    site = tmp_path / "site.json"
    site.write_text(json.dumps(SITE))
    for k, v in {"NETGALAXY_MODE": "sample", "NETGALAXY_SINKHOLE_IPS": "10.99.0.1", "NETGALAXY_ROUTER_IP": "10.20.0.1",
                 "NETGALAXY_LAN_CIDRS": "10.20.0.0/24", "NETGALAXY_VPN_CIDRS": "10.30.0.0/24", "NETGALAXY_WAN_INTERFACES": "wan0",
                 "NETGALAXY_BLOCKLIST_TRACKERS": "1770009021", "NETGALAXY_SYSLOG_PORT": "0",
                 "NETGALAXY_CONFIG_FILE": str(site), "NETGALAXY_DATA_DIR": str(tmp_path / "data")}.items():
        monkeypatch.setenv(k, v)
    from netgalaxy.config import load_settings
    return load_settings()


@pytest.fixture
def state(settings):
    from netgalaxy.state import NetState
    st = NetState(settings)
    st.inventory.created_at -= 3 * 86400          # past the learning day
    return st


def settle(st, rounds=2, gap=6.0):
    """Two readings a few seconds apart, as the live loop would take them, without waiting."""
    import time
    real = time.time
    base = real()
    try:
        for i in range(rounds):
            time.time = lambda i=i: base + i * gap
            graph = st.refresh_once()
    finally:
        time.time = real
    return graph
