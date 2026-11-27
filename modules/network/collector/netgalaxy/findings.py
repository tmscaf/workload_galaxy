# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""What looks wrong on the network, why it matters, and what to do about it.

Each finding names one subject (a device, a connection, the router), says in plain words what was
seen, and offers a next step. Nothing here changes the network: the app only watches.

Categories, in the order someone checking their network usually wants them:
    threat      a device is talking to an address on a known-bad list
    anomaly     a device is behaving unlike itself (upload volume, number of destinations)
    wifi        where a connection breaks: the internet line, the router, an access point, the air, a weak signal
    exposure    what the internet can reach through port forwards
    inbound     what the firewall is turning away at the door
    dns         lookups stopped by DNS blocking
    device      new and unidentified devices
    platform    the monitoring itself (ntopng, router logs)
"""
from __future__ import annotations

from .util import ago, plural, rate

CATEGORIES = [("threat", "Known-bad contact"), ("anomaly", "Unusual behaviour"), ("wifi", "Wi-Fi & connection"),
              ("exposure", "Open to the internet"),
              ("inbound", "Turned away"), ("dns", "Blocked lookups"), ("device", "Devices"), ("platform", "Monitoring")]
PATTERN_LABELS = {
    "exposure:forward": "ports open to the internet", "exposure:admin": "admin services open to the internet",
    "exposure:dead": "port forwards pointing at devices that are off", "device:unidentified": "devices that could not be identified",
    "device:new": "devices that joined in the last day", "dns:blocked": "devices with lookups being blocked",
    "anomaly:score": "devices ntopng scores as behaving unusually", "inbound:scan": "outside addresses probing many ports",
    "wifi:client": "devices with a poor Wi-Fi connection", "wifi:gear": "access points and switches with a problem",
}


class Diagnoser:
    def __init__(self, builder):
        self.b = builder
        self.s = builder.s
        self.findings = builder.findings

    def add(self, category: str, key: str, title: str, *, subject: str = "", severity: str | None = None,
            weight: float | None = None, detail: str = "", cause: str = "", fix: str = "", evidence=(), since_s: int | None = None,
            pattern: str | None = None, options: list[dict] | None = None, options_note: str = "",
            block: list[str] | None = None) -> dict:
        node = self.b.nodes.get(subject)
        edge = self.b.edges.get(subject)
        if edge:
            s, t = self.b.nodes.get(edge["source"], {}), self.b.nodes.get(edge["target"], {})
            ns = s.get("namespace") or t.get("namespace", "")
            name, kind = f"{s.get('name', '?')} → {t.get('name', '?')}", "call"
        else:
            ns = (node or {}).get("namespace", "")
            name, kind = (node or {}).get("name", ""), (node or {}).get("kind", "")
        sev = severity or ("critical" if (weight or 0) >= 0.6 else "warning" if (weight or 0) >= 0.25 else "info")
        fid = f"{category}:{key}"
        if fid in self.findings:
            return self.findings[fid]
        pat = pattern or f"{category}:{key.split(':', 1)[0]}"
        f = {"id": fid, "severity": sev, "category": category, "title": title, "subject": subject if (node or edge) else "",
             "subjectKind": kind, "subjectName": name, "namespace": ns, "platform": False, "pods": [], "podCount": 0,
             "detail": detail, "cause": cause, "fix": fix, "commands": [], "evidence": [e[:500] for e in evidence if e and e.split(":", 1)[-1].strip()][:6],
             "sinceS": since_s, "root": True, "causedBy": None, "causedByName": None, "blast": 0, "userFacing": False,
             "weight": round(weight or 0, 3), "pattern": pat, "patternLabel": PATTERN_LABELS.get(pat, "")}
        if block:
            # What a Block button on this finding would block, and what already is.
            f["block"] = [{"target": t, "blocked": self.b.blocked_by(t)} for t in block[:8]]
        if options:
            f["options"] = options[:5]
            if options_note:
                f["optionsNote"] = options_note
        self.findings[fid] = f
        if weight and (node or edge):
            (node or edge).setdefault("diag", []).append({"weight": weight, "text": title, "category": category})
        return f

    def run(self) -> None:
        for step in (self._threats, self._anomalies, self._wifi, self._exposure, self._inbound, self._dns, self._devices, self._monitoring):
            try:
                step()
            except Exception:  # noqa: BLE001 - one broken check must never take the map down
                import logging
                logging.getLogger(__name__).exception("check %s failed", step.__name__)

    def _devices_online(self) -> list[dict]:
        return [n for n in self.b.nodes.values() if n["kind"] == "workload" and n["device"]["present"] and not n.get("vpn")]

    # ------------------------------------------------------------------ known-bad contact
    def _threats(self) -> None:
        for e in list(self.b.edges.values()):
            t = self.b.nodes.get(e["target"], {})
            if e["type"] != "calls" or not (t.get("ext") or {}).get("blacklisted"):
                continue
            s = self.b.nodes[e["source"]]
            names = (t["ext"]["names"] or t["addresses"])[:3]
            self.add("threat", f"blacklist:{e['id']}", f"{s['name']} is talking to a known-bad address ({t['name']})",
                     subject=e["id"], weight=0.85,
                     detail=f"{s['name']} ({s['device']['ip']}) has {plural(e['traffic']['connections'], 'open connection')} to "
                            f"{', '.join(names)}, which is on a threat list ntopng tracks.",
                     cause="An address ends up on these lists for hosting malware, running a botnet's control server or scanning "
                           "the internet. A device contacting one is either infected, running something that phones home to a "
                           "bad host, or (less often) hitting a shared address that also serves something legitimate.",
                     fix="Find out what on the device is making the connection; if you cannot explain it, take the device off the network.",
                     evidence=[f"ports: {', '.join(e['ports'][:4])}", f"addresses: {', '.join(t['addresses'][:4])}"],
                     block=t["addresses"][:8],
                     options=[
                         {"title": "See exactly what it is doing", "detail": f"Open {s['name']} in ntopng and look at its active flows "
                          "to this address: the port and protocol usually identify the app.", "command": s["device"]["ntopngUrl"]},
                         {"title": "Block the address", "detail": "Use this finding's Block control: the router stops all traffic to and from it "
                          "within a minute, for every device. You choose how long; it can be undone.",
                          "note": "can block a legitimate site that shares an address with a bad one"},
                         {"title": "Isolate the device", "detail": "Disconnect it or block its address in Firewall > Rules > LAN until "
                          "it has been scanned or reset.", "admin": True}])

    # ------------------------------------------------------------------ unlike itself
    def _anomalies(self) -> None:
        for n in self._devices_online():
            d = n["device"]
            up, avg = d["upBps"] / 8, d["upAvgBps"] / 8                  # bytes per second
            enough = d["samples"] >= 120                                 # about an hour of readings
            if enough and up >= 500_000 and up > avg * 6:        # 4 Mbps and six times its own normal
                self.add("anomaly", f"upload:{n['id']}", f"{n['name']} is uploading far more than usual ({rate(up)})",
                         subject=n["id"], weight=0.45,
                         detail=f"Sending {rate(up)} now against a usual {rate(avg)} for this device.",
                         cause="Usually a backup, a cloud photo sync, a video call or a game upload. It is also what data being "
                               "copied off a device looks like, so it is worth knowing which.",
                         fix="Check where it is going: if the destination is a backup or cloud service you use, this is normal.",
                         evidence=[f"top destinations: {', '.join(self._destinations(n['id'])[:4]) or 'none in the busiest flows'}"],
                         options=[{"title": "See where the upload is going", "detail": "The device's connections on the map are sorted by "
                                   "traffic; the brightest line is the upload.", "command": d["ntopngUrl"]}])
            peers, pavg = d["peers"], d["peersAvg"]
            if enough and peers >= 150 and peers > max(pavg * 4, 60):
                self.add("anomaly", f"fanout:{n['id']}", f"{n['name']} is contacting an unusual number of hosts ({peers} at once)",
                         subject=n["id"], weight=0.4,
                         detail=f"{peers} outgoing connections now against a usual {pavg:.0f}.",
                         cause="File sharing (BitTorrent), a game or app update fetching from many mirrors, or a device scanning other "
                               "machines, which is what malware does when it spreads.",
                         fix="Look at what the connections have in common: one port across many addresses is a scan.",
                         options=[{"title": "Inspect the connections", "detail": "ntopng lists every flow with its port and protocol.",
                                   "command": d["ntopngUrl"]}])
            if d["ntopScore"] >= 1500:
                sev = "warning" if d["ntopScore"] >= 8000 else "info"
                self.add("anomaly", f"score:{n['id']}", f"ntopng scores {n['name']} as behaving unusually (score {d['ntopScore']})",
                         subject=n["id"], severity=sev, weight=0.3 if sev == "warning" else 0.0,
                         detail="ntopng adds to a device's score for each flow it finds odd: connections that fail, unusual ports, "
                                "TLS problems, suspicious names. A high score is a prompt to look, not a verdict.",
                         cause="Chatty phones, TVs and smart-home devices score high without anything being wrong.",
                         fix="Open the device in ntopng and read its alerts to see what the score is made of.",
                         pattern="anomaly:score",
                         options=[{"title": "Read the alerts behind the score", "detail": "The Alerts tab of the device page lists each one.",
                                   "command": d["ntopngUrl"]}])

    def _destinations(self, nid: str) -> list[str]:
        out = [e for e in self.b.edges.values() if e["source"] == nid and e["type"] == "calls"]
        out.sort(key=lambda e: -e["traffic"]["bytesPerSec"])
        return [self.b.nodes[e["target"]]["name"] for e in out]

    # ------------------------------------------------------------------ Wi-Fi and the path to the internet
    def _wifi(self) -> None:
        w = self.b.wifi
        if not w:
            return
        up = w["upstream"]
        if up["level"] in ("warn", "bad") and up["where"] != "none":
            bad = up["level"] == "bad"
            self.add("wifi", f"path:{up['where']}", up["title"], subject="host:router", severity="critical" if bad else "warning",
                     weight=0.65 if bad else 0.3, detail=up["detail"],
                     cause="Measured by pinging each hop every few seconds from a wired machine: the first hop that answers late "
                           "or not at all is where the trouble starts, and everything beyond it inherits it.",
                     fix=up["fix"] or "Open the Wi-Fi tab for the hop-by-hop figures and their history.",
                     evidence=[f"{h['name']} ({h['ip']}): {h['why']}" + (f", {h['hourLossPct']:.0f}% lost over the last hour"
                               if h.get("hourLossPct") else "") for h in up["hops"]])
        shared = {"router", "isp", "internet", "dns", "ap"}       # reported once above or per access point, not per device
        for g in w["gear"]:
            if g["level"] == "ok":
                continue
            nid = self.b.dev_by_mac.get(g["mac"]) or self.b.dev_by_ip.get(g["ip"]) or ""
            down = not g["online"]
            on_it = sum(1 for c in w["clients"] if c.get("ap") == g["mac"])
            self.add("wifi", f"gear:{g['mac']}", f"{g['name']}: {g['issues'][0] if g['issues'] else 'has a problem'}", subject=nid,
                     severity="critical" if down else "warning", weight=(0.6 if down else 0.3) if nid else None, pattern="wifi:gear",
                     detail=f"{g['kindLabel']} {g['name']} ({g['ip'] or 'no address'}): " + "; ".join(g["issues"]) + "."
                            + (f" {plural(on_it, 'Wi-Fi device')} on it right now." if on_it else ""),
                     cause="Everything connected through this device shares its problem, so fix it before looking at single devices.",
                     fix="Open the Wi-Fi tab: the access point's row shows its channels, how busy each is, and its link to the network.",
                     evidence=[f"{r['band']} channel {r['channel']}: {r['util']:.0f}% busy, {r['clients']} devices"
                               for r in g["radios"] if r.get("util") is not None])
        for c in w["clients"]:
            d = c["diag"]
            if d["level"] not in ("warn", "bad") or d["where"] in shared or d["where"] == "offline":
                continue
            nid = self.b.dev_by_mac.get(c["mac"]) or self.b.dev_by_ip.get(c.get("ip", "")) or ""
            bad = d["level"] == "bad"
            self.add("wifi", f"client:{c['mac']}", d["title"], subject=nid, severity="warning" if bad else "info",
                     weight=(0.3 if bad else 0.0) if nid else None, pattern="wifi:client", detail=d["detail"],
                     cause="Worked out from the access point's own figures for this device (signal, re-sent frames, drops) and "
                           "from pings along its path. The Wi-Fi tab shows the hops and 24 hours of history.",
                     fix=d["fix"], evidence=d["evidence"])

    # ------------------------------------------------------------------ open to the internet
    def _exposure(self) -> None:
        from .graph import ADMIN_PORTS
        for fw in self.b.forwards:
            e = fw.get("_entry")
            if not e:
                continue
            port, proto, name = e["port"], e["proto"].upper(), e["name"]
            nid = e["device"]
            n = self.b.nodes.get(nid) if nid else None
            target = f"{e['target']}:{e['targetPort']}"
            ports = [int(p) for p in str(port).replace(":", "-").split("-") if p.isdigit()]
            admin = next((ADMIN_PORTS[p] for p in (ports + [int(e["targetPort"])] if str(e["targetPort"]).isdigit() else ports)
                          if p in ADMIN_PORTS), None)
            where = n["name"] if n else e["target"]
            close = {"title": "Close it", "detail": f"Firewall > NAT > Port Forward: disable the '{name}' rule. Reach the service "
                     "over your VPN instead.", "admin": True}
            if admin:
                self.add("exposure", f"admin:{port}/{proto}:{e['target']}", f"{admin} on {where} is open to the whole internet (port {port})",
                         subject=nid or "", weight=0.7 if n else None, severity="critical",
                         detail=f"Internet port {port}/{proto} is forwarded to {target} ({name}).",
                         cause=f"{admin} is an administration service. Open to the internet it is found by scanners within minutes "
                               "and attacked with password guessing and known exploits around the clock.",
                         fix="Close the forward and use the VPN, or limit the rule's source to the addresses that need it.",
                         pattern="exposure:admin",
                         options=[close, {"title": "Limit who can reach it", "detail": "Edit the rule and set Source to the specific "
                                          "address or alias that needs access instead of 'any'.", "admin": True}])
            elif n is None or not n["device"]["present"]:
                self.add("exposure", f"dead:{port}/{proto}:{e['target']}", f"Port {port} is forwarded to {where}, which is not on the network",
                         subject=nid or "", severity="warning", weight=0.3 if n else None,
                         detail=f"Internet port {port}/{proto} is forwarded to {target} ({name}), and nothing is answering there"
                                + (f"; last seen {ago(n['device']['lastSeenS'])} ago." if n else "."),
                         cause="The server it was opened for is switched off or has moved. If addresses are handed out automatically, "
                               "another device can be given this address and become reachable from the internet without anyone meaning it.",
                         fix="Disable the forward until the server is back, and give servers a fixed address.",
                         pattern="exposure:dead",
                         options=[close, {"title": "Pin the address", "detail": "Services > DHCP Server > LAN: add a static mapping "
                                          "for the server so the address cannot go to another device.", "admin": True}])
            else:
                hits = self.b.edges.get(f"clients->{nid}", {}).get("traffic", {}).get("connections", 0)
                self.add("exposure", f"forward:{port}/{proto}:{e['target']}", f"Port {port} is open to the internet → {where} ({name})",
                         subject=nid, severity="info",
                         detail=f"Internet port {port}/{proto} is forwarded to {target}."
                                + (f" {plural(hits, 'outside connection')} open now." if hits else ""),
                         cause="Intended for a service you host. Listed so the full set of open doors is visible in one place.",
                         fix="Nothing to do if you still use it.", pattern="exposure:forward", options=[close])

    # ------------------------------------------------------------------ turned away at the door
    def _inbound(self) -> None:
        ev = self.b.events
        if not ev or not ev.get("events"):
            return
        mins = max(1, ev["windowSeconds"] // 60)
        if ev["blockedIn"]:
            top = ", ".join(f"{x['ip']} ({x['count']})" for x in ev["topSources"][:5])
            ports = ", ".join(f"{x['port']} ({x['count']})" for x in ev["topPorts"][:6])
            self.add("inbound", "blocked", f"{ev['blockedIn']:,} unwanted connection attempts turned away in the last {mins} min",
                     subject="host:router", severity="info",
                     detail=f"From {plural(ev['blockedInSources'], 'outside address', 'outside addresses')}; "
                            f"{ev['blockedInKnownBad']:,} of them from addresses on a known-bad list.",
                     cause="Background noise of the internet: scanners sweep every address all day. These never reached a device.",
                     fix="Nothing to do. Worth a look only if one source or one port dominates.",
                     evidence=[f"busiest sources: {top}", f"most-tried ports: {ports}"])
        for sc in ev.get("scanners", [])[:5]:
            self.add("inbound", f"scan:{sc['ip']}", f"{sc['ip']} probed {sc['ports']} different ports and was blocked",
                     subject="host:router", severity="info", pattern="inbound:scan",
                     detail="One outside address trying many ports is a port scan: someone mapping what your network exposes.",
                     block=[sc["ip"]],
                     cause="Automated scanning. It found only what your port forwards deliberately expose.",
                     fix="Nothing to do; the firewall dropped it.")
        for pair in ev.get("blockedOutPairs", []):
            if not pair.get("knownBad"):
                continue
            nid = self.b.dev_by_ip.get(pair["src"])
            name = self.b.nodes[nid]["name"] if nid else pair["src"]
            self.add("threat", f"blocked-out:{pair['src']}:{pair['dst']}", f"{name} tried to reach a known-bad address ({pair['dst']}) and was blocked",
                     subject=nid or "", weight=0.6 if nid else None, severity="warning",
                     detail=f"{pair['count']} attempts to {pair['dst']} port {pair['port']} in the last {mins} min, stopped by the firewall.",
                     block=[pair["dst"]],
                     cause="The block worked, but something on the device wanted that address: malware, an unwanted app, or an ad "
                           "library reaching a listed host.",
                     fix="Find what on the device is making the attempt.")

    # ------------------------------------------------------------------ DNS blocking
    def _dns(self) -> None:
        for n in self._devices_online():
            hits = n["device"]["dnsBlocked"]
            if hits < 3:
                continue
            noisy = hits >= 60
            self.add("dns", f"blocked:{n['id']}", f"{n['name']} keeps asking for blocked names ({hits} blocked connections now)",
                     subject=n["id"], severity="warning" if noisy else "info", weight=0.3 if noisy else 0.0, pattern="dns:blocked",
                     detail="The device looked up names on the block list and was sent to the blocking page instead.",
                     cause="A few is normal: apps and web pages load ads and trackers, and those are what the list mostly holds. "
                           "A steady stream from one device means an app that retries hard, or malware calling home to a blocked name."
                           if noisy else "Normal: ads and trackers in apps and web pages being stopped.",
                     fix="Firewall > pfBlockerNG > Reports > Alerts shows the exact names this device asked for.",
                     options=[{"title": "See which names", "detail": "pfBlockerNG > Reports > Alerts > DNSBL, filter by the device's "
                               f"address ({n['device']['ip']}).", "admin": True},
                              {"title": "Allow a name that should work", "detail": "In the same list, the + next to an entry adds it "
                               "to the whitelist.", "admin": True}])

    # ------------------------------------------------------------------ devices
    def _devices(self) -> None:
        learning = self.b.inv.learning
        for n in self._devices_online():
            d = n["device"]
            age = self.b.now - (d["firstSeen"] or self.b.now)
            if not learning and age < 86400:
                self.add("device", f"new:{n['id']}", f"New device on the network: {n['name']}", subject=n["id"], severity="info",
                         pattern="device:new", since_s=int(age),
                         detail=f"{d['typeLabel']} at {d['ip']}" + (f", made by {d['vendor']}" if d["vendor"] else "")
                                + (f", MAC {d['mac']}" if d["mac"] else "") + f". First seen {ago(age)} ago.",
                         cause="A device this app has not seen before joined. Phones and tablets that use a private Wi-Fi address "
                               "show up as new whenever that address rotates." if d["randomMac"] else
                               "A device this app has not seen before joined.",
                         fix="If you do not recognise it, check who is on your Wi-Fi and change the Wi-Fi password if needed.")
            if not d["named"] and not d["vendor"] and d["type"] == "unknown":
                self.add("device", f"unidentified:{n['id']}", f"Unidentified device at {d['ip']}", subject=n["id"], severity="info",
                         pattern="device:unidentified",
                         detail="No name, no known manufacturer and no recognisable behaviour yet" + (f" (MAC {d['mac']})." if d["mac"] else "."),
                         cause="The device has not announced a name and its hardware address is not in the manufacturer list.",
                         fix="Give it a name in the site file (devices: mac, name, type) once you know what it is.")

    # ------------------------------------------------------------------ the monitoring itself
    def _monitoring(self) -> None:
        src = self.b.sources
        nt = src.get("ntopng") or {}
        if nt.get("error"):
            self.add("platform", "ntopng", "Traffic data is not arriving from ntopng", severity="critical",
                     detail=nt["error"], cause="ntopng on the router is stopped, unreachable, or the app's login no longer works. "
                     "The map shows the last reading until it is back.",
                     fix="In pfSense, Status > Services: start ntopng if it is stopped (it has crashed before). "
                         "If it is running, check Diagnostics > ntopng Settings and the app's ntopng login.")
        sy = src.get("syslog") or {}
        if sy.get("enabled") and sy.get("error"):
            self.add("platform", "syslog-listen", "The firewall log receiver could not start", severity="warning", detail=sy["error"],
                     cause="Another process holds the port, or the app may not bind it.", fix="Check the app's port settings.")
        elif sy.get("enabled") and sy.get("uptimeS", 0) > 900 and (sy.get("lastMessageS") is None or sy["lastMessageS"] > 900):
            self.add("platform", "syslog-silent", "No firewall logs are arriving from the router", severity="warning",
                     detail="Nothing received " + (f"for {ago(sy['lastMessageS'])}." if sy.get("lastMessageS") is not None else "since the app started."),
                     cause="Remote logging is off on the router, points somewhere else, or the port is not reachable.",
                     fix="Status > System Logs > Settings on the router: enable remote logging to this app's address and port, "
                         "with Firewall Events and DHCP Events ticked.")
        wf = src.get("wifi") or {}
        if wf.get("enabled") and wf.get("error"):
            self.add("platform", "unifi", "Wi-Fi data is not arriving from the UniFi controller", severity="warning", detail=wf["error"],
                     cause="The controller is stopped or unreachable, or the app's login to it no longer works. Latency "
                           "measurements carry on; signal strength, access points and drops are missing until it is back.",
                     fix="Check that the controller is running and that the login in the Secret network-galaxy-unifi still works "
                         "(a View Only admin is enough).")
        pg = src.get("pings") or {}
        if pg.get("wanted") and pg.get("error"):
            self.add("platform", "pings", "Latency measurements are not running", severity="warning", detail=pg["error"],
                     cause="The app could not open the socket it pings with.",
                     fix="On Kubernetes add the pod sysctl net.ipv4.ping_group_range = \"0 2147483647\".")
