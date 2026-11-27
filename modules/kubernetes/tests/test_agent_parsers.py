# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("agent", ROOT / "agent" / "agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

SS = """LISTEN 0      4096         0.0.0.0:8080       0.0.0.0:*
\t cubic cwnd:10
LISTEN 3      128             [::]:5432          [::]:*
ESTAB  0      0      10.42.0.12:43210   10.43.0.180:3306
\t cubic wscale:7,7 rto:204 rtt:1.234/0.567 ato:40 mss:1448 bytes_sent:1234 bytes_acked:1235 bytes_received:5678 retrans:0/2 minrtt:0.5
ESTAB  1448   0      [::ffff:10.42.0.12]:8080   [::ffff:10.42.0.7]:51000
\t cubic rtt:0.2/0.1 bytes_received:100
SYN-SENT 0    1      10.42.0.12:40000   10.43.9.9:8080
TIME-WAIT 0   0      10.42.0.12:40001   10.43.9.9:8080
ESTAB  0      0      127.0.0.1:9000     127.0.0.1:41000
"""


def test_parse_ss_fields_and_states():
    socks = agent.parse_ss(SS)
    states = [s["state"] for s in socks]
    assert states.count("LISTEN") == 2 and "TIME-WAIT" not in states
    db = next(s for s in socks if s["rport"] == 3306)
    assert db["lip"] == "10.42.0.12" and db["rip"] == "10.43.0.180"
    assert db["rtt"] == 1.234 and db["rttvar"] == 0.567 and db["minrtt"] == 0.5
    assert db["retrans"] == 0 and db["retransTotal"] == 2
    assert db["bytesSent"] == 1234 and db["bytesReceived"] == 5678
    inbound = next(s for s in socks if s["lport"] == 8080 and s["state"] == "ESTAB")
    assert inbound["lip"] == "10.42.0.12" and inbound["rip"] == "10.42.0.7" and inbound["rq"] == 1448


def test_classify_direction_and_listen_queue():
    listen, conns = agent.classify(agent.parse_ss(SS))
    assert {"ip": "0.0.0.0", "port": 5432, "queue": 3, "backlog": 128} in [
        {**l, "ip": "0.0.0.0"} for l in listen if l["port"] == 5432]
    dirs = {(c["lport"], c["rport"]): c["dir"] for c in conns}
    assert dirs[(43210, 3306)] == "out"
    assert dirs[(8080, 51000)] == "in"
    assert dirs[(40000, 8080)] == "out"
    assert all(not c["rip"].startswith("127.") for c in conns)


def test_parse_proc_tcp():
    text = ("  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
            "   0: 0C002A0A:A8CA B4002B0A:0CEA 01 00000000:00000000 00:00000000 00000003  1000        0 1 1\n"
            "   1: 00000000:1F90 00000000:0000 0A 00000000:00000002 00:00000000 00000000  1000        0 2 1\n")
    socks = agent.parse_proc_tcp(text)
    est = socks[0]
    assert est["state"] == "ESTAB" and est["lip"] == "10.42.0.12" and est["lport"] == 43210
    assert est["rip"] == "10.43.0.180" and est["rport"] == 3306 and est["retrans"] == 3
    assert socks[1]["state"] == "LISTEN" and socks[1]["lport"] == 8080 and socks[1]["rq"] == 2


def test_parse_conntrack_proc_and_tool_formats():
    proc = ("ipv4     2 tcp      6 431999 ESTABLISHED src=10.42.0.12 dst=10.43.0.180 sport=43210 dport=3306 "
            "packets=10 bytes=900 src=10.42.0.11 dst=10.42.0.12 sport=3306 dport=43210 packets=8 bytes=1200 "
            "[ASSURED] mark=0 use=2\n"
            "ipv4     2 udp      17 29 src=203.0.113.50 dst=192.168.1.20 sport=50000 dport=2456 [UNREPLIED] "
            "src=10.42.0.15 dst=203.0.113.50 sport=2456 dport=50000 mark=0 use=1\n"
            "ipv4     2 tcp      6 10 ESTABLISHED src=127.0.0.1 dst=127.0.0.1 sport=1 dport=2 src=127.0.0.1 "
            "dst=127.0.0.1 sport=2 dport=1 mark=0 use=1\n")
    flows = agent.parse_conntrack(proc)
    assert len(flows) == 2
    tcp, udp = flows
    assert tcp["dst"] == "10.43.0.180" and tcp["dport"] == 3306 and tcp["rsrc"] == "10.42.0.11"
    assert tcp["bytes"] == 2100 and tcp["state"] == "ESTABLISHED" and not tcp["unreplied"]
    assert udp["proto"] == "udp" and udp["unreplied"] and udp["rsrc"] == "10.42.0.15" and udp["state"] == ""
    tool = "tcp      6 431999 ESTABLISHED src=10.42.0.12 dst=10.43.0.1 sport=5 dport=443 src=10.0.0.1 dst=10.42.0.12 sport=6443 dport=5 [ASSURED] mark=0 use=1\n"
    assert agent.parse_conntrack(tool)[0]["rsport"] == 6443


def test_conntrack_idle_time_from_remaining_ttl():
    t = {"tcp_est": 86400, "tcp_syn": 120, "udp": 30, "udp_stream": 120}
    text = ("tcp      6 86393 ESTABLISHED src=10.42.0.15 dst=198.51.100.98 sport=1 dport=27018 src=198.51.100.98 dst=192.168.1.20 sport=27018 dport=1 [ASSURED] mark=0 use=1\n"
            "tcp      6 400 ESTABLISHED src=10.42.0.15 dst=203.0.113.74 sport=2 dport=443 src=203.0.113.74 dst=192.168.1.20 sport=443 dport=2 [ASSURED] mark=0 use=1\n"
            "udp      17 113 src=10.42.0.15 dst=203.0.113.111 sport=3 dport=31187 src=203.0.113.111 dst=192.168.1.20 sport=31187 dport=3 [ASSURED] mark=0 use=1\n"
            "udp      17 24 src=10.42.0.15 dst=203.0.113.140 sport=4 dport=27033 [UNREPLIED] src=203.0.113.140 dst=192.168.1.20 sport=27033 dport=4 mark=0 use=1\n")
    flows = {f["dst"]: f for f in agent.parse_conntrack(text, t)}
    assert flows["198.51.100.98"]["idleS"] == 7          # active TCP
    assert flows["203.0.113.74"]["idleS"] == 86000      # dead for ~24 h: conntrack just hasn't expired it
    assert flows["203.0.113.111"]["idleS"] == 7 and flows["203.0.113.111"]["assured"]
    assert flows["203.0.113.140"]["idleS"] == 6 and flows["203.0.113.140"]["unreplied"]


def test_socket_idle_from_lastsnd_lastrcv():
    socks = agent.parse_ss("ESTAB 0 0 10.42.0.12:43210 10.43.0.180:3306\n\t cubic rtt:1/0.5 lastsnd:1500000 lastrcv:1499000 lastack:1499000\n")
    assert socks[0]["idleMs"] == 1499000
