#!/usr/bin/env python3
"""Workload Galaxy node agent (stdlib only).

Runs as a DaemonSet with hostPID + privileged. Every GALAXY_AGENT_INTERVAL
seconds it:
  1. finds one process per pod network namespace (pod UID from its cgroup),
  2. lists that namespace's TCP sockets with `ss -tinaH` via nsenter - kernel
     RTT, retransmits, send/recv queue depth, bytes; falls back to
     /proc/<pid>/net/tcp{,6} (no RTT) if nsenter/ss are unavailable,
  3. reads the node's conntrack table (TCP + UDP, original and reply tuples),
  4. POSTs the gzipped snapshot to the collector.

Nothing is written to the node and no packets are captured; only socket and
conntrack metadata is read.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request

PROC = os.environ.get("GALAXY_PROC", "/proc")
COLLECTOR = os.environ.get("GALAXY_COLLECTOR_URL", "http://workload-galaxy:8080").rstrip("/")
TOKEN = os.environ.get("GALAXY_AGENT_TOKEN", "")
NODE = os.environ.get("NODE_NAME") or socket.gethostname()
INTERVAL = max(5, int(os.environ.get("GALAXY_AGENT_INTERVAL", "10")))
CA_FILE = os.environ.get("GALAXY_CA_FILE", "")
MAX_SOCKETS_PER_NETNS = int(os.environ.get("GALAXY_MAX_SOCKETS_PER_NETNS", "3000"))
MAX_CONNTRACK = int(os.environ.get("GALAXY_MAX_CONNTRACK", "30000"))
# Same image as the collector: version/build are baked in by the Dockerfile.
VERSION = os.environ.get("GALAXY_VERSION", "dev")
BUILD = os.environ.get("GALAXY_BUILD", "")

log = logging.getLogger("galaxy-agent")
UID_RE = re.compile(r"pod([0-9a-f]{8}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{4}[-_][0-9a-f]{12})")
INFO_FIELDS = {
    "rtt": re.compile(r"\brtt:([\d.]+)/([\d.]+)"),
    "minrtt": re.compile(r"\bminrtt:([\d.]+)"),
    "retrans": re.compile(r"\bretrans:(\d+)/(\d+)"),
    "bytes_sent": re.compile(r"\bbytes_sent:(\d+)"),
    "bytes_received": re.compile(r"\bbytes_received:(\d+)"),
    "unacked": re.compile(r"\bunacked:(\d+)"),
    "lastsnd": re.compile(r"\blastsnd:(\d+)"),
    "lastrcv": re.compile(r"\blastrcv:(\d+)"),
}
KEEP_STATES = {"ESTAB", "SYN-SENT", "LISTEN"}
TCP_STATES = {"01": "ESTAB", "02": "SYN-SENT", "0A": "LISTEN"}


# --------------------------------------------------------------------------- pods
def pod_netns() -> list[tuple[int, str]]:
    """[(pid, pod uid)] - one process per non-host network namespace."""
    host_ns = os.readlink(f"{PROC}/1/ns/net")
    seen: dict[str, tuple[int, str]] = {}
    for pid in os.listdir(PROC):
        if not pid.isdigit():
            continue
        try:
            ns = os.readlink(f"{PROC}/{pid}/ns/net")
            if ns == host_ns or ns in seen:
                continue
            with open(f"{PROC}/{pid}/cgroup", encoding="utf-8") as fh:
                cgroup = fh.read()
        except OSError:
            continue
        m = UID_RE.search(cgroup)
        if m:
            seen[ns] = (int(pid), m.group(1).replace("_", "-"))
    return list(seen.values())


# --------------------------------------------------------------------------- sockets via ss
def _split_addr(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    host = host.strip("[]")
    if "%" in host:
        host = host.split("%", 1)[0]
    if host.lower().startswith("::ffff:") and "." in host:
        host = host[7:]
    if host in ("*", ""):
        host = "0.0.0.0"
    try:
        return host, int(port)
    except ValueError:
        return host, 0


def parse_ss(output: str) -> list[dict]:
    sockets: list[dict] = []
    current: dict | None = None
    for line in output.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t":
            if current is not None:
                info = line
                m = INFO_FIELDS["rtt"].search(info)
                if m:
                    current["rtt"], current["rttvar"] = float(m.group(1)), float(m.group(2))
                m = INFO_FIELDS["minrtt"].search(info)
                if m:
                    current["minrtt"] = float(m.group(1))
                m = INFO_FIELDS["retrans"].search(info)
                if m:
                    current["retrans"], current["retransTotal"] = int(m.group(1)), int(m.group(2))
                m = INFO_FIELDS["bytes_sent"].search(info)
                if m:
                    current["bytesSent"] = int(m.group(1))
                m = INFO_FIELDS["bytes_received"].search(info)
                if m:
                    current["bytesReceived"] = int(m.group(1))
                m = INFO_FIELDS["unacked"].search(info)
                if m:
                    current["unacked"] = int(m.group(1))
                # How long the connection has been quiet: an open-but-idle pool vs live traffic.
                last = [int(x.group(1)) for x in (INFO_FIELDS["lastsnd"].search(info),
                                                  INFO_FIELDS["lastrcv"].search(info)) if x]
                if last:
                    current["idleMs"] = min(last)
            continue
        parts = line.split()
        if len(parts) < 5:
            current = None
            continue
        state = parts[0]
        if state not in KEEP_STATES:
            current = None
            continue
        lip, lport = _split_addr(parts[3])
        rip, rport = _split_addr(parts[4])
        current = {"state": state, "rq": int(parts[1]), "sq": int(parts[2]),
                   "lip": lip, "lport": lport, "rip": rip, "rport": rport}
        sockets.append(current)
    return sockets


def _hex_ip(text: str) -> str:
    raw = bytes.fromhex(text)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    ip = socket.inet_ntop(socket.AF_INET6, words)
    return ip[7:] if ip.startswith("::ffff:") and "." in ip else ip


def parse_proc_tcp(text: str) -> list[dict]:
    out = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 7:
            continue
        state = TCP_STATES.get(parts[3])
        if not state:
            continue
        laddr, lport = parts[1].split(":")
        raddr, rport = parts[2].split(":")
        tx, rx = (int(x, 16) for x in parts[4].split(":"))
        out.append({"state": state, "rq": rx, "sq": tx, "lip": _hex_ip(laddr), "lport": int(lport, 16),
                    "rip": _hex_ip(raddr), "rport": int(rport, 16), "retrans": int(parts[6], 16)})
    return out


_ss_works: bool | None = None


def netns_sockets(pid: int) -> tuple[list[dict], str]:
    global _ss_works
    if _ss_works is not False:
        try:
            res = subprocess.run(["nsenter", "-t", str(pid), "-n", "ss", "-tinaH"],
                                 capture_output=True, text=True, timeout=5)
            if res.returncode == 0:
                _ss_works = True
                return parse_ss(res.stdout), "ss"
            if _ss_works is None:
                log.warning("ss via nsenter failed (%s); falling back to /proc (no RTT)", res.stderr.strip()[:200])
                _ss_works = False
        except (OSError, subprocess.TimeoutExpired) as err:
            if _ss_works is None:
                log.warning("ss via nsenter unavailable (%s); falling back to /proc (no RTT)", err)
                _ss_works = False
    sockets: list[dict] = []
    for fname in ("tcp", "tcp6"):
        try:
            with open(f"{PROC}/{pid}/net/{fname}", encoding="utf-8") as fh:
                sockets += parse_proc_tcp(fh.read())
        except OSError:
            pass
    return sockets, "procfs"


def classify(sockets: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split into (listen, connections) and tag connection direction."""
    listen = [s for s in sockets if s["state"] == "LISTEN"]
    ports = {s["lport"] for s in listen}
    conns = []
    for s in sockets:
        if s["state"] == "LISTEN":
            continue
        if s["rip"].startswith("127.") or s["rip"] == "::1":
            continue
        s["dir"] = "in" if s["lport"] in ports and s["state"] == "ESTAB" else "out"
        conns.append(s)
    return ([{"ip": s["lip"], "port": s["lport"], "queue": s["rq"], "backlog": s["sq"]} for s in listen],
            conns[:MAX_SOCKETS_PER_NETNS])


# --------------------------------------------------------------------------- conntrack
# The kernel refreshes an entry's timeout on every packet, so (configured timeout - remaining TTL)
# is the time since that flow last saw a packet. Without it, dead TCP flows linger for up to the
# established timeout (k3s: 24 h) and finished UDP flows for 30-120 s.
CT_TIMEOUT_KEYS = {"tcp_est": "nf_conntrack_tcp_timeout_established", "tcp_syn": "nf_conntrack_tcp_timeout_syn_sent",
                   "udp": "nf_conntrack_udp_timeout", "udp_stream": "nf_conntrack_udp_timeout_stream"}
CT_DEFAULT_TIMEOUTS = {"tcp_est": 432000, "tcp_syn": 120, "udp": 30, "udp_stream": 120}


def conntrack_timeouts() -> dict:
    """The node's (host netns) conntrack timeouts; defaults if they can't be read."""
    try:
        res = subprocess.run(["nsenter", "-t", "1", "-n", "cat"] +
                             [f"/proc/sys/net/netfilter/{k}" for k in CT_TIMEOUT_KEYS.values()],
                             capture_output=True, text=True, timeout=5)
        vals = res.stdout.split()
        if res.returncode == 0 and len(vals) == len(CT_TIMEOUT_KEYS):
            return dict(zip(CT_TIMEOUT_KEYS, (int(v) for v in vals)))
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return dict(CT_DEFAULT_TIMEOUTS)


def _ct_idle(proto: str, state: str, ttl: int | None, unreplied: bool, assured: bool, t: dict) -> int | None:
    if ttl is None:
        return None
    if proto == "udp":
        full = t["udp_stream"] if assured else t["udp"]
    elif state == "ESTABLISHED":
        full = t["tcp_est"]
    elif state == "SYN_SENT":
        full = t["tcp_syn"]
    else:
        return None     # closing states: the collector ignores them anyway
    return max(0, full - ttl)


def parse_conntrack(text: str, timeouts: dict | None = None) -> list[dict]:
    timeouts = timeouts or CT_DEFAULT_TIMEOUTS
    flows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 6:
            continue
        # /proc format starts "ipv4 2 tcp 6 ..."; `conntrack -L` starts "tcp 6 ..."
        if parts[0] in ("ipv4", "ipv6"):
            parts = parts[2:]
        proto = parts[0]
        if proto not in ("tcp", "udp"):
            continue
        state = parts[3] if proto == "tcp" and "=" not in parts[3] else ""
        orig: dict = {}
        reply: dict = {}
        total_bytes = None
        for token in parts:
            if token == "[UNREPLIED]":
                orig["unreplied"] = True
                continue
            if "=" not in token:
                continue
            k, v = token.split("=", 1)
            if k in ("src", "dst", "sport", "dport"):
                (orig if k not in orig else reply)[k] = v
            elif k == "bytes":
                total_bytes = (total_bytes or 0) + int(v)
        if not orig.get("src") or orig["src"].startswith("127.") or orig["src"] == "::1":
            continue
        unreplied = bool(orig.get("unreplied"))
        assured = "[ASSURED]" in parts
        ttl = _int(parts[2])
        flows.append({"proto": proto, "state": state, "src": orig.get("src"), "dst": orig.get("dst"),
                      "sport": _int(orig.get("sport")), "dport": _int(orig.get("dport")),
                      "rsrc": reply.get("src"), "rsport": _int(reply.get("sport")),
                      "unreplied": unreplied, "assured": assured, "bytes": total_bytes,
                      "idleS": _ct_idle(proto, state, ttl, unreplied, assured, timeouts)})
        if len(flows) >= MAX_CONNTRACK:
            break
    return flows


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def read_conntrack() -> tuple[list[dict], str | None]:
    path = f"{PROC}/1/net/nf_conntrack"
    timeouts = conntrack_timeouts()
    try:
        with open(path, encoding="utf-8") as fh:
            return parse_conntrack(fh.read(), timeouts), "procfs"
    except OSError:
        pass
    try:
        res = subprocess.run(["nsenter", "-t", "1", "-n", "conntrack", "-L", "-o", "extended"],
                             capture_output=True, text=True, timeout=10)
        if res.returncode == 0:
            return parse_conntrack(res.stdout, timeouts), "conntrack-tool"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return [], None


# --------------------------------------------------------------------------- report
def collect() -> dict:
    netns = []
    method = None
    for pid, uid in pod_netns():
        try:
            sockets, method = netns_sockets(pid)
        except Exception as err:  # noqa: BLE001 - one bad pod must not stop the report
            log.debug("pid %s: %s", pid, err)
            continue
        listen, conns = classify(sockets)
        netns.append({"podUid": uid, "pid": pid, "listen": listen, "sockets": conns})
    conntrack, ct_method = read_conntrack()
    return {"node": NODE, "agentVersion": VERSION, "agentBuild": BUILD, "ts": time.time(), "intervalSeconds": INTERVAL,
            "capabilities": {"sockets": method, "conntrack": ct_method}, "netns": netns, "conntrack": conntrack}


def send(report: dict) -> None:
    body = gzip.compress(json.dumps(report, separators=(",", ":")).encode())
    req = urllib.request.Request(f"{COLLECTOR}/api/v1/agent/report", data=body, method="POST", headers={
        "Content-Type": "application/json", "Content-Encoding": "gzip", "Authorization": f"Bearer {TOKEN}"})
    ctx = ssl.create_default_context(cafile=CA_FILE) if CA_FILE else None
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        resp.read()


def main() -> None:
    logging.basicConfig(level=os.environ.get("GALAXY_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN:
        raise SystemExit("GALAXY_AGENT_TOKEN is required")
    log.info("agent %s (build %s) on %s -> %s every %ss", VERSION, BUILD or "-", NODE, COLLECTOR, INTERVAL)
    cycle = 0
    while True:
        started = time.monotonic()
        try:
            report = collect()
            send(report)
            if cycle % 30 == 0:
                log.info("sent: %d pod netns, %d sockets, %d conntrack (%s/%s) in %.2fs",
                         len(report["netns"]), sum(len(n["sockets"]) for n in report["netns"]),
                         len(report["conntrack"]), report["capabilities"]["sockets"],
                         report["capabilities"]["conntrack"], time.monotonic() - started)
        except urllib.error.HTTPError as err:
            log.warning("collector rejected report: HTTP %s %s", err.code, err.read()[:200])
        except Exception as err:  # noqa: BLE001
            log.warning("cycle failed: %s: %s", type(err).__name__, err)
        cycle += 1
        time.sleep(max(1.0, INTERVAL - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
