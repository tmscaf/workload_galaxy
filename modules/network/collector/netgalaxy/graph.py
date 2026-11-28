# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Turn one reading of the network into the graph the front end draws.

The graph has the same shape as Workload Galaxy's, so the same 3D engine, search and tables work:

    Workload Galaxy          here
    ---------------          ----
    project / namespace      device group (Servers, Phones & tablets, Smart home, ...)
    workload                 a device on the network
    pod                      the address the device currently holds
    host / node              what the device hangs off: the router, or a hypervisor for its VMs
    external                 an internet service the devices talk to (grouped by service, not by address)
    clients                  the internet reaching in through port forwards
    calls edge               traffic from a device to a service or another device
    exposes edge             a port forward: the internet can open connections to this device
    "VPN connections" group  one node per person/device connected to the router's VPN right now; its
                             traffic to home devices is the traffic ntopng sees from its tunnel address
"""
from __future__ import annotations

import ipaddress
import re
from collections import Counter, defaultdict

from .config import Settings
from .devices import GROUPS, GROUP_TITLES, Inventory
from .findings import Diagnoser
from .graphops import summarize
from .util import iso_now, level_for, noisy_or, rate, slug, traffic_level

# Protocol labels that say how, not who: never used as a service name.
GENERIC_L7 = {
    "unknown", "tls", "quic", "http", "https", "dns", "ntp", "icmp", "icmpv6", "dhcp", "dhcpv6", "mdns", "ssdp", "ssh",
    "stun", "rtp", "rtcp", "netbios", "llmnr", "igmp", "snmp", "smbv1", "smbv23", "doh_dot", "http_proxy", "telnet",
    "rdp", "vnc", "mqtt", "syslog", "ipsec", "openvpn", "wireguard", "dtls", "ftp_control", "ftp_data", "imaps", "pops",
    "smtps", "smtp", "imap", "pop3", "ldap", "kerberos", "rtsp", "sip", "upnp", "wsd", "nat-pmp", "natpmp", "tftp",
    "bgp", "gre", "ocsp", "http_connect", "websocket", "cybersec", "ajp", "radius", "ip_icmp", "tcp", "udp",
}
SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu"}       # for names like example.co.uk
# Services opened to the whole internet that are aimed at administrators, not the public.
ADMIN_PORTS = {21: "FTP", 22: "SSH", 23: "Telnet", 135: "Windows RPC", 139: "NetBIOS", 445: "Windows file sharing",
               1433: "SQL Server", 1521: "Oracle", 2049: "NFS", 2375: "Docker API", 3306: "MySQL", 3389: "Remote Desktop",
               5432: "PostgreSQL", 5900: "VNC", 5985: "WinRM", 6379: "Redis", 8006: "Proxmox", 9200: "Elasticsearch",
               10000: "Webmin", 27017: "MongoDB"}


def registered_domain(name: str) -> str:
    """scontent.xx.fbcdn.net -> fbcdn.net; '' when the name is an address."""
    name = (name or "").strip(".").lower()
    if not name or re.match(r"^[\d.]+$", name) or ":" in name:
        return ""
    parts = name.split(".")
    if len(parts) < 2:
        return ""
    if len(parts) >= 3 and parts[-2] in SECOND_LEVEL and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def service_of(flow_l7: str, server_name: str, server_ip: str) -> tuple[str, str]:
    """-> (key, display name) of the internet service a flow belongs to."""
    l7 = flow_l7 or ""
    app = l7.split(".", 1)[1] if "." in l7 else l7
    if app and app.lower() not in GENERIC_L7:
        return f"app:{slug(app)}", app
    dom = registered_domain(server_name)
    if dom:
        return f"dom:{dom}", dom
    return f"ip:{server_ip}", server_ip


def _blank_latency() -> dict:
    return {"rttMinMs": None, "rttP50Ms": None, "rttP95Ms": None, "estimateMs": None, "basis": ""}


def _risk() -> dict:
    return {"score": 0.0, "level": "ok", "reasons": [], "impact": 0.0, "impactLevel": "ok", "impactFrom": None}


class Builder:
    def __init__(self, settings: Settings, inventory: Inventory, snap: dict, *, events: dict | None = None,
                 leases: dict | None = None, macs: dict | None = None, details: dict | None = None,
                 rates: dict | None = None, sources: dict | None = None, now: float = 0.0, alive: dict | None = None,
                 blocks: dict | None = None, vpn: dict | None = None, wifi: dict | None = None, plugins=None):
        self.s = settings
        self.plugins = plugins                # the module's plugins that are on; they may add findings
        self.inv = inventory
        self.snap = snap
        self.events = events or {}
        self.leases = leases or {}
        self.macs = macs or {}
        self.details = details or {}          # ip -> ntopng host detail
        self.rates = rates or {}              # ip -> (up bytes/s, down bytes/s) measured between refreshes
        self.sources = sources or {}
        self.alive = alive or {}              # ip -> answered a reachability check (port-forward targets, named servers)
        self.blocks = blocks or {}            # target -> block entry (what you chose to block)
        self.vpn = vpn or {}                  # VpnTracker.snapshot()
        self.wifi = wifi or {}                # WifiTracker.snapshot()
        self.wifi_by_mac = {c["mac"]: c for c in self.wifi.get("clients", []) if c.get("online")}
        self.wifi_by_ip = {c["ip"]: c for c in self.wifi_by_mac.values() if c.get("ip")}
        self.gear_by_ip = {g["ip"]: g for g in self.wifi.get("gear", []) if g.get("ip")}
        self.dev_by_mac: dict[str, str] = {}  # MAC -> node id
        self.now = now
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.findings: dict[str, dict] = {}
        self.dev_by_ip: dict[str, str] = {}   # ip -> node id
        self.sinkhole_hits: Counter = Counter()
        self.sinkhole = set(settings.sinkhole_ips)
        self.site = settings.site or {}
        self.forwards = [f for f in self.site.get("forwards", []) if f.get("target") and f.get("port")]

    # ------------------------------------------------------------------ helpers
    def _skip_local(self, h: dict) -> bool:
        ip = h.get("ip", "")
        if h.get("is_broadcast") or h.get("is_multicast") or ip in ("0.0.0.0", "::") or ip in self.sinkhole:
            return True
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return True
        if a.is_multicast or a.is_link_local or a.is_unspecified or a.is_loopback or a.version == 6:
            return True        # IPv6 link-local chatter belongs to devices we already hold by IPv4
        return ip.endswith(".255") or ip == self.s.router_ip

    def _host_for(self, desc: dict) -> str:
        for h in self.site.get("hosts", []):
            if any(desc["mac"].startswith(o.upper()) for o in h.get("ouis", []) if desc["mac"]) or desc["ip"] in h.get("ips", []):
                return f"host:{slug(h.get('id') or h.get('name'))}"
        return "host:router"

    # ------------------------------------------------------------------ build
    def build(self) -> dict:
        self._devices()
        self._vpn_sessions()
        self._hosts()
        self._flows()
        self._forwards()
        diag = Diagnoser(self)
        diag.run()
        self._score()
        return self._assemble()

    def _devices(self) -> None:
        s, now = self.s, self.now
        active: dict[str, dict] = {}
        for h in self.snap.get("hosts", []):
            ip = h.get("ip", "")
            if not s.is_lan(ip) or self._skip_local(h) or s.is_vpn(ip):
                continue
            active[ip] = h
        seen_keys = set()
        for ip, h in active.items():
            det = self.details.get(ip) or {}
            lease = self.leases.get(ip) or {}
            mac = (det.get("mac") or lease.get("mac") or "").upper()
            if mac in ("00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF"):
                mac = ""
            # What the Wi-Fi controller knows about this address: its MAC, and a name someone gave it there.
            known = self.wifi_by_ip.get(ip) or self.gear_by_ip.get(ip) or {}
            mac = mac or known.get("mac", "")
            alias = known.get("alias") or (known.get("name", "") if known.get("kind") else "")
            up, down = self.rates.get(ip, (0.0, 0.0))
            idle_s = now - (h.get("last_seen") or now)
            self.inv.observe(ip, now=now, name=h.get("name", ""), mac=mac, vendor=(self.macs.get(mac) or {}).get("vendor", ""),
                             model=(self.macs.get(mac) or {}).get("model", ""),
                             ntop_type=det.get("devtype") or None, dhcp_name=lease.get("name", ""), up_bps=up, down_bps=down,
                             peers=(h.get("num_flows") or {}).get("as_client", 0), active=idle_s < s.present_seconds,
                             alias=alias)
            seen_keys.add(self.inv.by_ip[ip])
        # A server that answered a reachability check is on the network even when it has been too
        # quiet for ntopng to list it.
        for ip, up in self.alive.items():
            if up and s.is_lan(ip) and ip != s.router_ip and (ip not in active or self.inv.by_ip.get(ip) not in seen_keys):
                lease = self.leases.get(ip) or {}
                self.inv.observe(ip, now=now, mac=(lease.get("mac") or "").upper(), dhcp_name=lease.get("name", ""), active=True)
                seen_keys.add(self.inv.by_ip[ip])
        for key, d in self.inv.items():
            last = d.get("lastSeen", d.get("firstSeen", now))
            present = key in seen_keys and now - last < s.present_seconds
            if not present and now - last > s.offline_hours * 3600:
                continue
            desc = self.inv.describe(key)
            ip = desc["ip"]
            if not ip or self.dev_by_ip.get(ip) or s.is_vpn(ip):
                continue       # the address has since gone to another device, which is the one drawn
            h = active.get(ip) if present else None
            up, down = self.rates.get(ip, (0.0, 0.0)) if present else (0.0, 0.0)
            host_id = self._host_for(desc)
            nid = f"wl:{desc['group']}/Device/{slug(key)}"
            age = int(now - d.get("firstSeen", now))
            score = (h or {}).get("score", {}).get("total", 0) if h else 0
            dev = {"ip": ip, "mac": desc["mac"], "vendor": desc["vendor"], "model": desc["model"], "type": desc["type"],
                   "typeLabel": desc["typeLabel"], "checked": self.alive.get(ip),
                   "named": desc["named"], "randomMac": desc["randomMac"], "present": present,
                   "firstSeen": d.get("firstSeen"), "lastSeen": last, "lastSeenS": round(now - last),
                   "upBps": round(up * 8), "downBps": round(down * 8), "upText": rate(up), "downText": rate(down),
                   "upAvgBps": round(d.get("upAvg", 0) * 8), "downAvgBps": round(d.get("downAvg", 0) * 8),
                   "bytesSent": (h or {}).get("bytes", {}).get("sent", 0), "bytesRecvd": (h or {}).get("bytes", {}).get("recvd", 0),
                   "flows": (h or {}).get("num_flows", {}).get("total", 0), "peers": (h or {}).get("num_flows", {}).get("as_client", 0),
                   "peersAvg": round(d.get("peersAvg", 0), 1), "samples": d.get("samples", 0), "ntopScore": score,
                   "blacklisted": bool((h or {}).get("is_blacklisted")), "dnsBlocked": 0, "apps": [], "forwards": [],
                   "ntopngUrl": f"{self.s.ntopng_url}/lua/host_details.lua?host={ip}" if self.s.ntopng_url else ""}
            w = self.wifi_by_mac.get(desc["mac"]) or self.wifi_by_ip.get(ip)
            if w:
                dev["wifi"] = {"mac": w["mac"], "ap": w.get("apName", ""), "ssid": w.get("ssid", ""), "band": w.get("band", ""),
                               "channel": w.get("channel"), "proto": w.get("proto", ""), "signal": w.get("signal"),
                               "retryPct": w.get("retryPct"), "txMbps": w.get("txMbps"), "rxMbps": w.get("rxMbps"),
                               "drops24h": w.get("drops24h", 0), "roams24h": w.get("roams24h", 0),
                               "pingMs": (w.get("ping") or {}).get("p50Ms"), "lossPct": (w.get("ping") or {}).get("lossPct"),
                               "level": w["diag"]["level"], "where": w["diag"]["where"], "title": w["diag"]["title"]}
            pod = {"containerInfo": [], "owner": "", "ownerKind": "", "notReadyS": None, "deletingS": None, "claims": [],
                   "name": ip, "phase": "Running", "ready": True, "restarts": 0, "recentRestarts": 0, "reason": "", "message": "",
                   "node": host_id.split(":", 1)[1], "ip": ip, "hostId": host_id, "hostNetwork": False, "terminal": False,
                   "deleting": False, "qos": "", "containers": 1, "cpu": None, "mem": None, "cpuLimit": None,
                   "cpuRequest": None, "memLimit": None, "memRequest": None, "ageSeconds": age, "level": "ok", "issues": []}
            self.nodes[nid] = {
                "id": nid, "kind": "workload", "subkind": desc["typeLabel"], "name": desc["name"], "namespace": desc["group"],
                "platform": False, "replicas": {"desired": 1 if present else 0, "ready": 1 if present else 0, "current": 1 if present else 0},
                "pods": [pod] if present else [], "services": [], "images": [], "hosts": [pod["node"]] if present else [],
                "hpa": None, "completed": False, "group": desc["group"],
                "rollout": {"state": "complete", "revision": 1, "ageSeconds": age, "updated": 1, "updatedReady": 1, "desired": 1,
                            "old": 0, "changes": [], "strategy": "", "message": ""},
                "storage": [], "usage": {"cpuRatio": None, "memRatio": None}, "idle": not present or (up + down) < 200,
                "risk": _risk(), "ghost": False, "device": dev, "_host": host_id}
            self.dev_by_ip[ip] = nid
            if desc["mac"]:
                self.dev_by_mac[desc["mac"]] = nid

    def _vpn_sessions(self) -> None:
        """One node per VPN session, in the "VPN connections" group, hanging off the router."""
        now = self.now
        for v in self.vpn.get("sessions", []):
            tunnel = v.get("tunnel") or ""
            who = v.get("user") or v.get("cert") or "unknown user"
            what = v.get("platform") or "device"
            nid = f"wl:vpn/Session/{slug(v['key'])}"
            name = f"{who} · {what}"
            up, down = v.get("inBps", 0) / 8, v.get("outBps", 0) / 8     # the client's upload is what the server received
            since = v.get("since") or now
            info = {**{k: v.get(k) for k in ("key", "user", "cert", "real", "realPort", "tunnel", "platform", "app", "cipher", "server",
                                             "bytesIn", "bytesOut", "since", "durationS", "where", "confirmed")},
                    "upBps": v.get("inBps", 0), "downBps": v.get("outBps", 0)}
            dev = {"ip": tunnel or v.get("real", ""), "mac": "", "vendor": "", "model": v.get("app") or "", "type": "vpn",
                   "typeLabel": "VPN session", "checked": None, "named": True, "randomMac": False, "present": True,
                   "firstSeen": since, "lastSeen": now, "lastSeenS": 0, "upBps": round(up * 8), "downBps": round(down * 8),
                   "upText": rate(up), "downText": rate(down), "upAvgBps": 0, "downAvgBps": 0,
                   "bytesSent": v.get("bytesIn", 0), "bytesRecvd": v.get("bytesOut", 0), "flows": 0, "peers": 0, "peersAvg": 0,
                   "samples": 0, "ntopScore": 0, "blacklisted": False, "dnsBlocked": 0, "apps": [], "forwards": [], "ntopngUrl": ""}
            pod = {"containerInfo": [], "owner": "", "ownerKind": "", "notReadyS": None, "deletingS": None, "claims": [],
                   "name": tunnel or v.get("real", ""), "phase": "Running", "ready": True, "restarts": 0, "recentRestarts": 0,
                   "reason": "", "message": "", "node": "router", "ip": tunnel, "hostId": "host:router", "hostNetwork": False,
                   "terminal": False, "deleting": False, "qos": "", "containers": 1, "cpu": None, "mem": None, "cpuLimit": None,
                   "cpuRequest": None, "memLimit": None, "memRequest": None, "ageSeconds": int(now - since), "level": "ok", "issues": []}
            self.nodes[nid] = {
                "id": nid, "kind": "workload", "subkind": "VPN session", "name": name, "namespace": "vpn", "platform": False,
                "replicas": {"desired": 1, "ready": 1, "current": 1}, "pods": [pod], "services": [], "images": [], "hosts": ["router"],
                "hpa": None, "completed": False, "group": "vpn",
                "rollout": {"state": "complete", "revision": 1, "ageSeconds": int(now - since), "updated": 1, "updatedReady": 1,
                            "desired": 1, "old": 0, "changes": [], "strategy": "", "message": ""},
                "storage": [], "usage": {"cpuRatio": None, "memRatio": None}, "idle": (up + down) < 200,
                "risk": _risk(), "ghost": False, "device": dev, "vpn": info, "_host": "host:router"}
            if tunnel:
                self.dev_by_ip[tunnel] = nid

    def _hosts(self) -> None:
        iface = self.snap.get("interface") or {}
        ev = self.events
        counts = Counter(n["_host"] for n in self.nodes.values() if n["device"]["present"])

        def host(hid: str, name: str, role: str, extra: dict) -> None:
            self.nodes[hid] = {
                "id": hid, "kind": "host", "subkind": role, "name": name, "namespace": "", "ready": True, "readyStatus": "True",
                "readyMessage": "", "pressure": [], "taints": [], "zone": "", "roles": [role.lower()], "cpuAllocatable": 0.0,
                "memAllocatable": 0.0, "cpuCapacity": 0.0, "memCapacity": 0.0, "cpuUsed": 0.0, "memUsed": 0.0,
                "podCount": counts.get(hid, 0), "cpuRequested": 0.0, "memRequested": 0.0, "memLimits": 0.0,
                "podsWithoutMemLimit": 0, "kubelet": "", "os": "", "unschedulable": False,
                "net": {"connections": 0, "retransConnections": 0, "slowConnections": 0, "retransShare": 0.0, "slowShare": 0.0,
                        "reportingNodes": 1, "rttP50Ms": None, "rttP95Ms": None, "clusterRttP50Ms": None},
                "agentStale": False, "cpuRatio": None, "cpuRequestRatio": None, "memRatio": None, "memRequestRatio": None,
                "memLimitRatio": None, "memSystemReserved": 0.0, "risk": {"score": 0.0, "level": "ok", "reasons": []},
                "ghost": False, "infra": extra}

        host("host:router", self.s.router_name, "Router", {
            "ip": self.s.router_ip, "throughputBps": round(iface.get("throughput_bps") or 0), "flows": iface.get("num_flows", 0),
            "hosts": iface.get("num_hosts", 0), "localHosts": iface.get("num_local_hosts", 0), "devices": iface.get("num_devices", 0),
            "drops": iface.get("drops", 0), "blockedIn": ev.get("blockedIn", 0), "blockedInSources": ev.get("blockedInSources", 0),
            "blockedInKnownBad": ev.get("blockedInKnownBad", 0), "blockedOut": ev.get("blockedOut", 0)})
        for h in self.site.get("hosts", []):
            hid = f"host:{slug(h.get('id') or h.get('name'))}"
            if hid not in self.nodes:
                host(hid, h.get("name") or hid, h.get("role") or "Hypervisor", {"ip": (h.get("ips") or [""])[0]})

    def _edge(self, src: str, dst: str, kind: str) -> dict:
        eid = f"{src}->{dst}"
        e = self.edges.get(eid)
        if e is None:
            e = self.edges[eid] = {
                "id": eid, "source": src, "target": dst, "type": kind, "evidence": [], "via": [], "ports": [], "details": [],
                "traffic": {"connections": 0, "bytesPerSec": 0.0, "lastActiveS": None, "idle": True, "level": 0.0, "bytes": 0},
                "latency": _blank_latency(), "health": {"retrans": 0, "retransTotal": 0, "sendQ": 0, "recvQ": 0, "pending": 0},
                "risk": {"score": 0.0, "level": "ok", "reasons": []}, "_apps": Counter(), "_ports": Counter(), "_names": Counter()}
        return e

    def _add_flow(self, e: dict, f: dict, port: str, app: str, name: str) -> None:
        if "observed" not in e["evidence"]:
            e["evidence"].append("observed")
        t = e["traffic"]
        bps = ((f.get("thpt") or {}).get("bps") or 0.0) / 8.0
        t["connections"] += 1
        t["bytesPerSec"] += bps
        t["bytes"] += f.get("bytes") or 0
        idle = self.now - (f.get("last_seen") or self.now)
        t["lastActiveS"] = idle if t["lastActiveS"] is None else min(t["lastActiveS"], idle)
        e["_ports"][port] += 1
        if app:
            e["_apps"][app] += f.get("bytes") or 0
        if name:
            e["_names"][name] += f.get("bytes") or 0

    def _flows(self) -> None:
        ext: dict[str, dict] = {}
        inbound_sources: Counter = Counter()
        for f in self.snap.get("flows", []):
            c, sv = f.get("client") or {}, f.get("server") or {}
            cip, sip = c.get("ip", ""), sv.get("ip", "")
            l7 = (f.get("protocol") or {}).get("l7", "")
            l4 = (f.get("protocol") or {}).get("l4", "")
            port = f"{sv.get('port')}/{l4}" if sv.get("port") else l4
            cdev, sdev = self.dev_by_ip.get(cip), self.dev_by_ip.get(sip)
            if sip in self.sinkhole and cdev:
                self.sinkhole_hits[cdev] += 1
                continue
            c_lan, s_lan = self.s.is_lan(cip) or self.s.is_vpn(cip), self.s.is_lan(sip) or self.s.is_vpn(sip)
            app = l7.split(".", 1)[1] if "." in l7 else (l7 if l7.lower() not in GENERIC_L7 else "")
            if cdev and not s_lan:
                key, title = service_of(l7, sv.get("name", ""), sip)
                x = ext.setdefault(key, {"key": key, "name": title, "ips": set(), "names": Counter(), "bytes": 0, "bps": 0.0,
                                         "flows": [], "blacklisted": False, "apps": Counter()})
                x["ips"].add(sip)
                x["bytes"] += f.get("bytes") or 0
                x["bps"] += ((f.get("thpt") or {}).get("bps") or 0.0) / 8.0
                x["blacklisted"] |= bool(sv.get("is_blacklisted"))
                if sv.get("name") and sv["name"] != sip:
                    x["names"][sv["name"]] += 1
                if app:
                    x["apps"][app] += 1
                x["flows"].append((cdev, f, port, app, sv.get("name") if sv.get("name") != sip else ""))
                self.nodes[cdev]["device"]["apps"].append((app or registered_domain(sv.get("name", "")) or "", f.get("bytes") or 0))
            elif sdev and not c_lan:
                # The internet opening a connection to a device: only possible through a port forward.
                e = self._edge("clients", sdev, "exposes")
                self._add_flow(e, f, port, app, "")
                inbound_sources[cip] += 1
            elif cdev and sdev and cdev != sdev:
                self._add_flow(self._edge(cdev, sdev, "calls"), f, port, app, "")
        # Draw the busiest services one by one; fold the long tail into a single node.
        ranked = sorted(ext.values(), key=lambda x: (x["blacklisted"], x["bps"], x["bytes"]), reverse=True)
        keep, rest = ranked[: self.s.external_limit], ranked[self.s.external_limit:]
        for x in keep:
            self._external(f"ext:{x['key']}", x["name"], [x])
        if rest:
            self._external("ext:other", f"{len(rest)} other internet services", rest)
        self.inbound_sources = inbound_sources
        for n in self.nodes.values():
            if n["kind"] == "workload":
                apps = Counter()
                for a, b in n["device"]["apps"]:
                    if a:
                        apps[a] += b
                n["device"]["apps"] = [{"name": a, "bytes": b} for a, b in apps.most_common(8)]
                n["device"]["dnsBlocked"] = self.sinkhole_hits.get(n["id"], 0)

    def _external(self, nid: str, name: str, members: list[dict]) -> None:
        ips = sorted({ip for x in members for ip in x["ips"]})
        names = Counter()
        apps = Counter()
        for x in members:
            names.update(x["names"])
            apps.update(x["apps"])
        black = any(x["blacklisted"] for x in members)
        many = len(members) > 1
        blocked = [self.blocked_by(ip) for ip in ips]
        # Offer a Block button only where it is safe to block every address: a bare address, or a
        # service on a threat list. A named service (a CDN, a cloud) shares its addresses with others.
        offer = ips[:8] if not many and (members[0]["key"].startswith("ip:") or black) else []
        self.nodes[nid] = {
            "id": nid, "kind": "external", "subkind": "External", "name": name, "namespace": "", "addresses": ips[:12],
            "scope": "internet",
            "scopeWhy": (f"{len(members)} services with little traffic each, shown as one" if many else
                         f"public internet service ({len(ips)} address{'es' if len(ips) != 1 else ''} in use)"),
            "risk": _risk(), "ghost": False,
            "ext": {"services": len(members), "addressCount": len(ips), "names": [n for n, _ in names.most_common(8)],
                    "apps": [a for a, _ in apps.most_common(6)], "blacklisted": black,
                    "bytesPerSec": round(sum(x["bps"] for x in members), 1), "bytes": sum(x["bytes"] for x in members),
                    "members": [x["name"] for x in members[:25]] if many else [],
                    "blockTargets": offer, "blocked": [b for b in blocked if b][:8],
                    "allBlocked": bool(ips) and all(blocked)}}
        for x in members:
            for cdev, f, port, app, sname in x["flows"]:
                self._add_flow(self._edge(cdev, nid, "calls"), f, port, app, sname)

    def blocked_by(self, ip: str) -> dict | None:
        if not self.blocks:
            return None
        if ip in self.blocks:
            return self.blocks[ip]
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return None
        return next((e for k, e in self.blocks.items() if "/" in k and a in ipaddress.ip_network(k, strict=False)), None)

    def _forwards(self) -> None:
        public = self.site.get("publicAddress", "")
        for fw in self.forwards:
            nid = self.dev_by_ip.get(fw["target"])
            label = f"{fw['port']}/{(fw.get('proto') or 'tcp').upper()}"
            entry = {"name": fw.get("name") or f"port {fw['port']}", "port": str(fw["port"]), "proto": (fw.get("proto") or "tcp").lower(),
                     "target": fw["target"], "targetPort": str(fw.get("targetPort") or fw["port"]), "device": nid}
            fw["_entry"] = entry
            if not nid:
                continue
            n = self.nodes[nid]
            n["device"]["forwards"].append(entry)
            n["services"].append({"name": entry["name"], "type": "PortForward", "clusterIP": "", "ports": [label],
                                  "lbIPs": [public] if public else [], "readyEndpoints": 1 if n["device"]["present"] else 0,
                                  "notReadyEndpoints": 0 if n["device"]["present"] else 1})
            e = self._edge("clients", nid, "exposes")
            if "declared" not in e["evidence"]:
                e["evidence"].append("declared")
            e["via"].append(entry["name"])
            e["details"].append(f"Internet port {label} → {fw['target']}:{entry['targetPort']} ({entry['name']})")
        if any(e["source"] == "clients" for e in self.edges.values()):
            top = [ip for ip, _ in getattr(self, "inbound_sources", Counter()).most_common(8)]
            self.nodes["clients"] = {
                "id": "clients", "kind": "clients", "subkind": "Internet", "name": "The internet (inbound)", "namespace": "",
                "scope": "internet", "seenFrom": top,
                "scopeWhy": "anyone on the internet opening a connection through one of your port forwards",
                "risk": _risk(), "ghost": False}

    # ------------------------------------------------------------------ scoring and assembly
    def _score(self) -> None:
        for obj in list(self.nodes.values()) + list(self.edges.values()):
            diag = obj.pop("diag", [])
            if not diag:
                continue
            diag.sort(key=lambda d: -d["weight"])
            score = noisy_or([d["weight"] for d in diag])
            obj["risk"].update(score=round(score, 3), level=level_for(score),
                               reasons=[{"text": d["text"], "weight": d["weight"], "category": d["category"]} for d in diag[:6]])
            for p in obj.get("pods", []):
                p["level"] = obj["risk"]["level"]
                p["issues"] = [d["text"] for d in diag[:3]]

    def _assemble(self) -> dict:
        for e in self.edges.values():
            t = e["traffic"]
            t["bytesPerSec"] = round(t["bytesPerSec"], 1)
            t["idle"] = t["bytesPerSec"] < 50 and (t["lastActiveS"] is None or t["lastActiveS"] > 60)
            t["level"] = round(traffic_level(t["bytesPerSec"]), 3)
            t["lastActiveS"] = round(t["lastActiveS"], 1) if t["lastActiveS"] is not None else None
            e["ports"] = [p for p, _ in e.pop("_ports").most_common(6)] + e["ports"]
            apps, names = e.pop("_apps"), e.pop("_names")
            e["net"] = {"apps": [a for a, _ in apps.most_common(5)], "names": [n for n, _ in names.most_common(5)],
                        "rateText": rate(t["bytesPerSec"])}
            if apps and not e["details"]:
                e["details"] = [", ".join(a for a, _ in apps.most_common(4))]
        for n in self.nodes.values():
            n.pop("_host", None)
            if n.get("vpn") and not (n["device"]["upBps"] or n["device"]["downBps"]):
                # The VPN server's speed needs two reports; until then, what ntopng sees through the tunnel.
                out = sum(e["traffic"]["bytesPerSec"] for e in self.edges.values() if e["source"] == n["id"])
                into = sum(e["traffic"]["bytesPerSec"] for e in self.edges.values() if e["target"] == n["id"])
                n["device"].update(upBps=round(out * 8), downBps=round(into * 8), upText=rate(out), downText=rate(into))
                n["idle"] = (out + into) < 200
        findings = sorted(self.findings.values(), key=lambda f: ({"critical": 0, "warning": 1, "info": 2}[f["severity"]], -f["weight"]))
        nodes, edges = list(self.nodes.values()), list(self.edges.values())
        per_group: dict[str, list[dict]] = defaultdict(list)
        for n in nodes:
            if n["kind"] == "workload":
                per_group[n["namespace"]].append(n)
        fcount: dict[str, Counter] = defaultdict(Counter)
        for f in findings:
            if f["namespace"]:
                fcount[f["namespace"]][f["severity"]] += 1
        namespaces = []
        for gid, title in GROUPS:
            ms = per_group.get(gid, [])
            if not ms:
                continue
            present = [m for m in ms if m["device"]["present"]]
            crit = sum(1 for m in ms if m["risk"]["level"] == "critical")
            warn = sum(1 for m in ms if m["risk"]["level"] == "warning")
            top = next((f for f in findings if f["namespace"] == gid and f["severity"] != "info"), None)
            namespaces.append({
                "name": gid, "title": title, "group": gid, "platform": False, "workloads": len(ms), "pods": len(present),
                "warning": warn, "critical": crit, "healthy": len(ms) - warn - crit, "degraded": 0, "down": 0,
                "podsReady": len(present), "restarts1h": 0, "rolling": 0,
                "findings": {k: fcount[gid].get(k, 0) for k in ("critical", "warning", "info")},
                "top": {"title": top["title"], "severity": top["severity"], "id": top["id"]} if top else None, "quota": None,
                "online": len(present), "offline": len(ms) - len(present),
                "upBps": sum(m["device"]["upBps"] for m in ms), "downBps": sum(m["device"]["downBps"] for m in ms)})
        hot = [{"id": n["id"], "kind": n["kind"], "name": n["name"], "namespace": n.get("namespace", ""), "score": n["risk"]["score"],
                "level": n["risk"]["level"], "reasons": n["risk"]["reasons"][:3]}
               for n in nodes if n["risk"]["level"] != "ok"]
        hot += [{"id": e["id"], "kind": "call", "name": f"{self.nodes[e['source']]['name']} → {self.nodes[e['target']]['name']}",
                 "namespace": self.nodes[e["source"]].get("namespace", ""), "score": e["risk"]["score"], "level": e["risk"]["level"],
                 "reasons": e["risk"]["reasons"][:3]} for e in edges if e["risk"]["level"] != "ok"]
        hot.sort(key=lambda h: -h["score"])
        busiest = sorted((e for e in edges if e["type"] == "calls"), key=lambda e: -e["traffic"]["bytesPerSec"])[:8]
        paths = [{"nodes": [e["source"], e["target"]], "edges": [e["id"]], "score": e["traffic"]["level"], "latencyMs": None,
                  "measuredHops": 0, "hops": 1, "bytesPerSec": e["traffic"]["bytesPerSec"], "rateText": e["net"]["rateText"],
                  "name": f"{self.nodes[e['source']]['name']} → {self.nodes[e['target']]['name']}"}
                 for e in busiest if e["traffic"]["bytesPerSec"] > 0]
        iface = self.snap.get("interface") or {}
        devices = [n for n in nodes if n["kind"] == "workload" and not n.get("vpn")]     # VPN sessions are counted apart
        online = [n for n in devices if n["device"]["present"]]
        talkers = sorted(online, key=lambda n: -(n["device"]["upBps"] + n["device"]["downBps"]))[:8]
        services = sorted((n for n in nodes if n["kind"] == "external"), key=lambda n: -n["ext"]["bytesPerSec"])[:8]
        network = {
            "name": self.s.network_name, "devices": len(devices), "online": len(online), "offline": len(devices) - len(online),
            "throughputBps": round(iface.get("throughput_bps") or 0),
            "uploadBps": sum(n["device"]["upBps"] for n in online), "downloadBps": sum(n["device"]["downBps"] for n in online),
            "flows": iface.get("num_flows", 0), "remoteHosts": max(0, (iface.get("num_hosts") or 0) - (iface.get("num_local_hosts") or 0)),
            "services": sum(1 for n in nodes if n["kind"] == "external"), "forwards": len(self.forwards),
            "learning": self.inv.learning,
            "vpn": {"connected": self.vpn.get("connected", 0), "failed24h": self.vpn.get("failed24h", 0),
                    "probes24h": self.vpn.get("probes24h", 0)} if self.vpn else None,
            "wifi": {**self.wifi["summary"], "enabled": self.wifi["enabled"], "level": self.wifi["upstream"]["level"],
                     "title": self.wifi["upstream"]["title"],
                     "hops": [{"id": h["id"], "name": h["name"], "state": h["state"], "p50Ms": h.get("p50Ms"), "lossPct": h.get("lossPct")}
                              for h in self.wifi["upstream"]["hops"]]} if self.wifi else None,
            "topTalkers": [{"id": n["id"], "name": n["name"], "upBps": n["device"]["upBps"], "downBps": n["device"]["downBps"]} for n in talkers],
            "topServices": [{"id": n["id"], "name": n["name"], "bps": round(n["ext"]["bytesPerSec"] * 8)} for n in services],
            "firewall": self.events or None}
        graph = {
            "generatedAt": iso_now(), "cluster": {"name": self.s.network_name, "openshift": False, "nodes": sum(1 for n in nodes if n["kind"] == "host")},
            "sources": self.sources, "hotspots": hot[:20], "paths": paths, "cascades": [], "findings": findings, "changes": self._changes(devices),
            "platform": {"components": [], "nodes": {"total": sum(1 for n in nodes if n["kind"] == "host"),
                                                     "ready": sum(1 for n in nodes if n["kind"] == "host"), "pressure": [], "cordoned": 0}, "cli": ""},
            "namespaces": namespaces, "externalHosts": [], "nodes": nodes, "edges": edges, "network": network}
        # The full graph keeps every finding: each API response groups its own scope into patterns
        # (graphops.filter_graph), so only the totals are added here.
        graph.update({k: v for k, v in summarize(findings).items() if k != "findings"})
        return graph

    def _changes(self, devices: list[dict]) -> list[dict]:
        """Recent comings and goings, newest first."""
        if self.inv.learning:
            return []
        out = []
        for n in devices:
            if n.get("vpn"):
                continue
            d = n["device"]
            age = self.now - (d["firstSeen"] or self.now)
            if age < 86400:
                out.append({"ageS": int(age), "kind": "joined", "subject": n["id"], "namespace": n["namespace"], "name": n["name"],
                            "title": f"{n['name']} joined the network", "detail": f"{d['typeLabel']} at {d['ip']}"
                            + (f", {d['vendor']}" if d["vendor"] else "")})
        return sorted(out, key=lambda c: c["ageS"])[:30]


def group_title(gid: str) -> str:
    return GROUP_TITLES.get(gid, gid)
