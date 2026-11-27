# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Turn one reading of the host into the graph the front end draws.

    graph format             here
    ------------             ----
    namespace                a group of virtual machines (from the site file, else by guest OS)
    workload                 a virtual machine
    pod                      what the VM uses: its slot on the host while it runs, and each virtual disk
    host                     the ESXi host
    external, scope storage  a datastore
    external, scope lan      a port group (a virtual network VMs plug into); its details are under node["pg"]
    external, scope uplink   a physical network card carrying a virtual switch's traffic out of the host
    "attached" edge          a VM's network adapter on a port group; flow = the VM's network rate
    "stores" edge            a VM's disks on a datastore; flow = the VM's disk rate
    "uplink" edge            a port group leaving the host through a physical card

A powered-off VM stays on the map, dimmed, with its disks: it still holds space.
"""
from __future__ import annotations

import fnmatch
import math

from galaxy_core import nodes as N
from galaxy_core.graphops import summarize
from galaxy_core.util import iso_now, slug

from .findings import CATEGORIES, Checks, PATTERN_LABELS

GB = 2**30
MB = 2**20
FAMILY_GROUPS = [("linux", "Linux VMs"), ("windows", "Windows VMs"), ("unix", "BSD & Unix VMs"), ("other", "Other VMs"),
                 ("templates", "Templates")]
TOOLS_LABEL = {"guestToolsCurrent": "current", "guestToolsUnmanaged": "open-vm-tools (managed by the guest)",
               "guestToolsNeedUpgrade": "out of date", "guestToolsSupportedOld": "old but supported",
               "guestToolsSupportedNew": "newer than the host", "guestToolsTooOld": "too old", "guestToolsTooNew": "newer than the host",
               "guestToolsBlacklisted": "known-bad version", "guestToolsNotInstalled": "not installed", "": "unknown"}


def gb(n: float | None, digits: int = 1) -> str:
    """Bytes as vSphere shows them: binary gigabytes, labelled GB."""
    n = float(n or 0)
    if n >= 1024 * GB:
        return f"{n / (1024 * GB):.2f} TB"
    if n >= 10 * GB or digits == 0:
        return f"{n / GB:.0f} GB"
    if n >= GB:
        return f"{n / GB:.{digits}f}".rstrip("0").rstrip(".") + " GB"
    return f"{n / MB:.0f} MB"


def pct(x: float | None) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


class Builder:
    def __init__(self, settings, snap: dict, *, history, sources: dict, now: float):
        self.s, self.snap, self.history, self.sources, self.now = settings, snap, history, sources, now
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.findings = N.Findings(self.nodes, self.edges, PATTERN_LABELS)
        self.groups: list[tuple[str, str]] = []
        self.host_id: dict[str, str] = {}        # host moid -> node id
        self.ds_id: dict[str, str] = {}          # datastore name -> node id
        self.pg_id: dict[str, str] = {}          # port group name -> node id
        self.nic_id: dict[str, str] = {}         # physical NIC device -> node id
        self.vm_id: dict[str, str] = {}          # VM moid -> node id

    # ------------------------------------------------------------------ build
    def build(self) -> dict:
        self._hosts()
        self._datastores()
        self._networks()
        self._vms()
        Checks(self).run()
        N.score(list(self.nodes.values()) + list(self.edges.values()))
        return self._assemble()

    # ------------------------------------------------------------------ groups
    def _group_of(self, vm: dict) -> str:
        if vm["template"]:
            return "templates"
        for g in self.s.site.get("groups") or []:
            if any(fnmatch.fnmatchcase(vm["name"].lower(), str(p).lower()) for p in g.get("match") or []):
                return slug(g.get("id") or g.get("title") or "group")
        return vm["family"] if vm["family"] in dict(FAMILY_GROUPS) else "other"

    def _group_list(self) -> list[tuple[str, str]]:
        site = [(slug(g.get("id") or g.get("title") or "group"), g.get("title") or g.get("id") or "Group")
                for g in self.s.site.get("groups") or []]
        seen = {gid for gid, _t in site}
        return site + [(gid, title) for gid, title in FAMILY_GROUPS if gid not in seen]

    # ------------------------------------------------------------------ host
    def _hosts(self) -> None:
        vms = self.snap["vms"]
        for h in self.snap["hosts"]:
            hid = f"host:{slug(h['name']) if h['name'] and not h['name'].startswith('localhost') else slug(self.s.host or h['id'])}"
            if hid in self.nodes:
                hid = f"{hid}-{slug(h['id'])}"
            self.host_id[h["id"]] = hid
            mine = [v for v in vms if (v.get("hostId") or h["id"]) == h["id"]]
            on = [v for v in mine if v["powerState"] == "poweredOn"]
            mhz_total = (h["cores"] or 1) * (h["cpuMhz"] or 1)
            p = h.get("perf") or {}
            esx = {
                "moid": h["id"], "hostName": h["name"], "vendor": _real(h["vendor"]), "model": _real(h["model"]),
                "cpuModel": h["cpuModel"],
                "sockets": h["sockets"], "cores": h["cores"], "threads": h["threads"], "cpuMhz": h["cpuMhz"],
                "memBytes": h["memBytes"], "memUsedBytes": h["memUsedBytes"], "cpuUsedMhz": h["cpuUsedMhz"],
                "cpuPct": round(h["cpuUsedMhz"] / mhz_total, 3), "memPct": round(h["memUsedBytes"] / (h["memBytes"] or 1), 3),
                "uptimeS": h["uptimeS"], "product": h["product"], "version": (h["product"] or {}).get("version", ""),
                "build": (h["product"] or {}).get("build", ""), "licence": self.snap["about"].get("licence", ""),
                "maintenance": h["maintenance"], "status": h["overallStatus"], "lockdown": h["lockdown"],
                "clockSkewS": h["clockSkewS"], "ntp": h["ntp"],
                "services": [x for x in h["services"] if x["key"] in ("TSM-SSH", "TSM", "ntpd", "DCUI", "snmpd", "vmsyslogd")],
                "firewall": {"enabled": sum(1 for r in h["firewall"]["rulesets"] if r["enabled"]),
                             "total": len(h["firewall"]["rulesets"]),
                             "open": [{"key": r["key"], "label": r["label"], "allIp": r["allIp"], "allowed": r["allowed"][:6],
                                       "ports": [f"{x['port']}/{x['protocol']}" for x in r["rules"] if x["direction"] == "inbound"][:4]}
                                      for r in h["firewall"]["rulesets"] if r["enabled"]
                                      and any(x["direction"] == "inbound" for x in r["rules"])]},
                "sensors": h["sensors"][:40], "vswitches": h["vswitches"], "portgroups": h["portgroups"], "pnics": h["pnics"],
                "vmknics": h["vmknics"], "vms": len(mine), "vmsOn": len(on),
                "vcpus": sum(v["numCpu"] for v in on), "memAllocated": sum(v["memMB"] for v in on) * MB,
                "memAllocatedAll": sum(v["memMB"] for v in mine) * MB,
                "netRxBps": round((p.get("netRxKbps") or 0) * 1024 * 8), "netTxBps": round((p.get("netTxKbps") or 0) * 1024 * 8),
                "diskReadBps": round((p.get("diskReadKbps") or 0) * 1024), "diskWriteBps": round((p.get("diskWriteKbps") or 0) * 1024),
                "diskLatencyMs": p.get("diskLatencyMs"),
            }
            esx["vcpuRatio"] = round(esx["vcpus"] / (h["cores"] or 1), 2)
            esx["memAllocRatio"] = round(esx["memAllocated"] / (h["memBytes"] or 1), 3)
            name = self.s.name or (h["name"] if not h["name"].startswith("localhost") else self.s.host or h["name"])
            self.nodes[hid] = N.host(
                hid, name, "ESXi host", cpu_capacity=float(h["cores"]), mem_capacity=float(h["memBytes"]),
                cpu_used=round(h["cpuUsedMhz"] / (h["cpuMhz"] or 1), 2), mem_used=float(h["memUsedBytes"]),
                cpu_requested=float(esx["vcpus"]), mem_requested=float(esx["memAllocated"]), pod_count=len(on),
                ready=h["connectionState"] == "connected" and not h["maintenance"],
                ready_message="in maintenance mode" if h["maintenance"] else "", os=(h["product"] or {}).get("fullName", ""),
                pinned=True, esx=esx)

    # ------------------------------------------------------------------ datastores
    def _datastores(self) -> None:
        for d in self.snap["datastores"]:
            nid = f"ext:ds:{slug(d['name'])}"
            self.ds_id[d["name"]] = nid
            used = max(0, d["capacity"] - d["free"])
            provisioned = used + d["uncommitted"]
            cap = d["capacity"] or 1
            self.nodes[nid] = N.external(
                nid, d["name"], "Datastore", scope="storage",
                why=f"{d['type'] or 'datastore'} · {gb(used)} of {gb(d['capacity'])} used",
                ds={"moid": d["id"], "type": d["type"], "accessible": d["accessible"], "capacity": d["capacity"], "free": d["free"],
                    "used": used, "usedRatio": round(used / cap, 3), "provisioned": provisioned,
                    "provisionedRatio": round(provisioned / cap, 3), "vms": d["vms"], "maintenance": d["maintenance"],
                    "bytesPerSec": 0.0, "snapshotBytes": 0, "offBytes": 0})

    # ------------------------------------------------------------------ port groups and uplinks
    def _networks(self) -> None:
        for h in self.snap["hosts"]:
            uplinks = {v["name"]: v["uplinks"] for v in h["vswitches"]}
            pnic = {n["device"]: n for n in h["pnics"]}
            vmk = {k["portgroup"]: k for k in h["vmknics"]}
            for sw in h["vswitches"]:
                for dev in sw["uplinks"]:
                    n = pnic.get(dev)
                    if not n or dev in self.nic_id:
                        continue
                    nid = f"ext:nic:{slug(dev)}"
                    self.nic_id[dev] = nid
                    speed = f"{n['speedMb'] / 1000:g} Gbps" if n["speedMb"] >= 1000 else f"{n['speedMb']} Mbps"
                    self.nodes[nid] = N.external(
                        nid, dev, "Uplink", scope="uplink",
                        why=f"physical network card on {sw['name']} · {speed if n['linkUp'] else 'no link'}",
                        nic={**n, "vswitch": sw["name"], "speedText": speed if n["linkUp"] else "no link", "bytesPerSec": 0.0})
            for g in h["portgroups"]:
                nid = f"ext:net:{slug(g['name'])}"
                if nid in self.nodes:
                    continue
                self.pg_id[g["name"]] = nid
                vlan = "all VLANs (trunk)" if g["vlan"] == 4095 else f"VLAN {g['vlan']}" if g["vlan"] else "no VLAN tag"
                k = vmk.get(g["name"])
                self.nodes[nid] = N.external(
                    nid, g["name"], "Port group", scope="lan", why=f"{g['vswitch']} · {vlan}",
                    pg={"vswitch": g["vswitch"], "vlan": g["vlan"], "vlanText": vlan, "security": g["security"],
                         "uplinks": uplinks.get(g["vswitch"], []), "kernel": g["kernel"], "vmk": k["device"] if k else "",
                         "vmkIp": k["ip"] if k else "", "vms": 0, "bytesPerSec": 0.0},
                    addresses=[k["ip"]] if k and k["ip"] else [])

    # ------------------------------------------------------------------ virtual machines
    def _vms(self) -> None:
        host_mhz = {h["id"]: h["cpuMhz"] or 1 for h in self.snap["hosts"]}
        default_host = self.snap["hosts"][0]["id"] if self.snap["hosts"] else ""
        used_ids: set[str] = set()
        for v in sorted(self.snap["vms"], key=lambda x: x["name"].lower()):
            group = self._group_of(v)
            nid = f"wl:{group}/VM/{slug(v['name'])}"
            if nid in used_ids:
                nid = f"{nid}-{slug(v['id'])}"
            used_ids.add(nid)
            self.vm_id[v["id"]] = nid
            on = v["powerState"] == "poweredOn"
            hmoid = v.get("hostId") or default_host
            hid = self.host_id.get(hmoid, "")
            p, q = v.get("perf") or {}, v["quick"]
            cpu_pct = (p["cpuUsagePct"] / 10000) if p.get("cpuUsagePct") is not None else \
                (q["cpuMhz"] / v["maxCpuMhz"] if v["maxCpuMhz"] else 0.0)
            cpu_pct = max(0.0, min(1.0, cpu_pct)) if on else 0.0
            ready = (p["cpuReadyMs"] / (20000 * max(1, v["numCpu"]))) if on and p.get("cpuReadyMs") is not None else None
            active_mb = (p["memActiveKb"] / 1024) if p.get("memActiveKb") is not None else q["guestMemMB"]
            active_mb = active_mb if on else 0
            rx = (p.get("netRxKbps") or 0) * 1024 if on else 0.0        # bytes per second
            tx = (p.get("netTxKbps") or 0) * 1024 if on else 0.0
            dr = (p.get("diskReadKbps") or 0) * 1024 if on else 0.0
            dw = (p.get("diskWriteKbps") or 0) * 1024 if on else 0.0
            hist = self.history.stats(v["id"]) if self.history else {}
            snaps = v["snapshots"]
            oldest = min((s["created"] for s in snaps if s["created"]), default=None)
            vm = {
                "moid": v["id"], "power": v["powerState"], "on": on, "template": v["template"], "os": v["guestFullName"],
                "family": v["family"], "hostname": v["hostname"], "ips": v["ips"], "ip": (v["ips"] or [""])[0],
                "annotation": v["annotation"][:400], "cpus": v["numCpu"], "memMB": v["memMB"],
                "cpuPct": round(cpu_pct, 4), "cpuMhz": q["cpuMhz"] if on else 0,
                "readyPct": round(ready, 4) if ready is not None else None,
                "memActiveMB": round(active_mb), "memActivePct": round(active_mb / v["memMB"], 4) if v["memMB"] and on else None,
                "memHostMB": q["hostMemMB"] if on else 0, "balloonMB": q["balloonMB"], "swappedMB": q["swappedMB"],
                "swapInKbps": p.get("swapInKbps") or 0,
                "netRxBps": round(rx * 8), "netTxBps": round(tx * 8), "diskReadBps": round(dr), "diskWriteBps": round(dw),
                "diskLatencyMs": p.get("diskLatencyMs") if on else None,
                "provisioned": v["provisioned"], "committed": v["committed"], "uncommitted": v["uncommitted"],
                "snapshotCount": len(snaps), "snapshotBytes": v["snapshotBytes"],
                "snapshotOldestDays": round((self.now - oldest) / 86400, 1) if oldest else None,
                "snapshots": [{"name": s["name"], "ageDays": round((self.now - s["created"]) / 86400, 1) if s["created"] else None,
                               "depth": s["depth"], "memory": s["memory"], "description": s["description"][:200]} for s in snaps[:20]],
                "consolidationNeeded": v["consolidationNeeded"], "question": v["question"],
                "tools": v["tools"], "toolsLabel": TOOLS_LABEL.get(v["tools"]["status"], v["tools"]["status"]),
                "toolsRunning": v["tools"]["running"] == "guestToolsRunning",
                "nics": v["nics"], "disks": v["disks"], "cdroms": v["cdroms"], "guestDisks": _filesystems(v["guestDisks"]),
                "datastores": v["datastores"], "networks": v["networks"], "uptimeS": v["uptimeS"] if on else None,
                "hwVersion": v["hwVersion"], "firmware": v["firmware"], "changed": v["changed"],
                "peakCpuPct": hist.get("peakCpuPct"), "peakMemActiveMB": hist.get("peakMemActiveMB"),
                "watchedS": hist.get("watchedS", 0),
            }
            pods = []
            if on and hid:
                mhz = host_mhz.get(hmoid, 1)
                pods.append(N.pod(vm["ip"] or "running", hid, ip=vm["ip"], age=int(v["uptimeS"] or 0),
                                  cpu=round(q["cpuMhz"] / mhz, 3), cpu_limit=float(v["numCpu"]),
                                  mem=float(active_mb * MB), mem_limit=float(v["memMB"] * MB), part="compute",
                                  summary=f"{v['numCpu']} vCPU · {gb(v['memMB'] * MB, 0)}"))
            for d in v["disks"]:
                ds = self.ds_id.get(d["datastore"], "")
                if ds:
                    pods.append(N.pod(f"{d['label']} · {gb(d['capacity'], 0)}", ds, age=0, part="disk",
                                      summary=("thin" if d["thin"] else "thick") + f" on {d['datastore']}"))
            self.nodes[nid] = N.workload(
                nid, v["name"], group, "Template" if v["template"] else "Virtual machine", pods=pods, running=on,
                age=int(v["uptimeS"] or 0), cpu_ratio=round(cpu_pct, 3) if on else None,
                mem_ratio=round(active_mb / v["memMB"], 3) if on and v["memMB"] else None,
                idle=not on or (cpu_pct < 0.01 and rx + tx < 200), vm=vm)

            # Network adapters: one line per port group.
            by_pg: dict[str, list[dict]] = {}
            for nic in v["nics"]:
                by_pg.setdefault(nic["network"], []).append(nic)
            live = sum(1 for nic in v["nics"] if nic["connected"]) or 1
            for pg, nics in by_pg.items():
                target = self.pg_id.get(pg)
                if not target:
                    continue
                connected = [n for n in nics if n["connected"]]
                flow = (rx + tx) * len(connected) / live
                self.edges[f"{nid}->{target}"] = N.edge(
                    nid, target, "attached", bytes_per_sec=flow, observed=on and bool(connected), connections=len(nics),
                    details=[f"{n['label']} · {n['type']} · {n['mac']}" + ("" if n["connected"] or not on else " · not connected")
                             for n in nics], net={"rateText": "", "apps": [], "names": []})
                self.nodes[target]["pg"]["vms"] += 1
                self.nodes[target]["pg"]["bytesPerSec"] += flow

            # Disks: one line per datastore.
            total = sum(d["capacity"] for d in v["disks"]) or 1
            by_ds: dict[str, list[dict]] = {}
            for d in v["disks"]:
                by_ds.setdefault(d["datastore"], []).append(d)
            for ds, disks in by_ds.items():
                target = self.ds_id.get(ds)
                if not target:
                    continue
                share = sum(d["capacity"] for d in disks) / total
                flow = (dr + dw) * share
                self.edges[f"{nid}->{target}"] = N.edge(
                    nid, target, "stores", bytes_per_sec=flow, observed=on, connections=len(disks),
                    details=[f"{d['label']} · {gb(d['capacity'], 0)} · {'thin' if d['thin'] else 'thick'}" for d in disks],
                    net={"rateText": "", "apps": [], "names": []})
                dsn = self.nodes[target]["ds"]
                dsn["bytesPerSec"] += flow
                dsn["snapshotBytes"] += int(v["snapshotBytes"] * share)
                if not on:
                    dsn["offBytes"] += int(v["committed"] * share)

        # Port groups leave the host through their virtual switch's physical cards.
        for pg, target in self.pg_id.items():
            net = self.nodes[target]["pg"]
            ups = [u for u in net["uplinks"] if u in self.nic_id]
            for u in ups:
                nic = self.nodes[self.nic_id[u]]["nic"]
                flow = net["bytesPerSec"] / len(ups)
                self.edges[f"{target}->{self.nic_id[u]}"] = N.edge(
                    target, self.nic_id[u], "uplink", bytes_per_sec=flow, observed=nic["linkUp"],
                    details=[f"{net['vswitch']} · {nic['speedText']}"], net={"rateText": "", "apps": [], "names": []})
                nic["bytesPerSec"] += flow

    # ------------------------------------------------------------------ assembly
    def _assemble(self) -> dict:
        findings = self.findings.sorted()
        nodes, edges = list(self.nodes.values()), list(self.edges.values())
        for e in edges:
            e["net"]["rateText"] = _rate(e["traffic"]["bytesPerSec"], disk=e["type"] == "stores")
        vms = [n for n in nodes if n["kind"] == "workload"]
        on = [n for n in vms if n["vm"]["on"]]
        hosts = [n for n in nodes if n["kind"] == "host"]
        dss = [n for n in nodes if n.get("ds")]

        def extra(_gid: str, ms: list[dict]) -> dict:
            live = [m for m in ms if m["vm"]["on"]]
            return {"vcpus": sum(m["vm"]["cpus"] for m in live), "memMB": sum(m["vm"]["memMB"] for m in live),
                    "committed": sum(m["vm"]["committed"] for m in ms),
                    "cpuMhz": sum(m["vm"]["cpuMhz"] for m in live), "memActiveMB": sum(m["vm"]["memActiveMB"] for m in live)}

        namespaces = N.group_rows(nodes, findings, self._group_list(), extra)
        busiest = sorted((e for e in edges if e["type"] != "uplink"), key=lambda e: -e["traffic"]["bytesPerSec"])[:8]
        paths = [{"nodes": [e["source"], e["target"]], "edges": [e["id"]], "score": e["traffic"]["level"], "latencyMs": None,
                  "measuredHops": 0, "hops": 1, "bytesPerSec": e["traffic"]["bytesPerSec"], "rateText": e["net"]["rateText"],
                  "name": f"{self.nodes[e['source']]['name']} → {self.nodes[e['target']]['name']}"}
                 for e in busiest if e["traffic"]["bytesPerSec"] > 0]
        h = hosts[0]["esx"] if hosts else {}
        cap = sum(d["ds"]["capacity"] for d in dss)
        top = lambda key, k=6: [{"id": n["id"], "name": n["name"], "value": n["vm"][key]} for n in    # noqa: E731
                                sorted(on, key=lambda n: -(n["vm"][key] or 0))[:k] if n["vm"][key]]
        estate = {
            "name": hosts[0]["name"] if hosts else (self.s.name or self.s.host),
            "hosts": len(hosts), "vms": len(vms), "on": len(on), "off": len(vms) - len(on),
            "templates": sum(1 for n in vms if n["vm"]["template"]),
            "cores": sum(x["esx"]["cores"] for x in hosts), "threads": sum(x["esx"]["threads"] for x in hosts),
            "vcpus": sum(n["vm"]["cpus"] for n in on), "vcpuRatio": h.get("vcpuRatio"),
            "cpuPct": h.get("cpuPct"), "memPct": h.get("memPct"),
            "memBytes": sum(x["esx"]["memBytes"] for x in hosts), "memUsedBytes": sum(x["esx"]["memUsedBytes"] for x in hosts),
            "memAllocated": sum(n["vm"]["memMB"] for n in on) * MB, "memAllocRatio": h.get("memAllocRatio"),
            "memActive": sum(n["vm"]["memActiveMB"] for n in on) * MB,
            "storage": {"capacity": cap, "used": sum(d["ds"]["used"] for d in dss), "free": sum(d["ds"]["free"] for d in dss),
                        "provisioned": sum(d["ds"]["provisioned"] for d in dss), "datastores": len(dss),
                        "usedRatio": round(sum(d["ds"]["used"] for d in dss) / cap, 3) if cap else None},
            "snapshots": {"count": sum(n["vm"]["snapshotCount"] for n in vms), "bytes": sum(n["vm"]["snapshotBytes"] for n in vms),
                          "vms": sum(1 for n in vms if n["vm"]["snapshotCount"])},
            "offBytes": sum(n["vm"]["committed"] for n in vms if not n["vm"]["on"]),
            "netRxBps": sum(n["vm"]["netRxBps"] for n in on), "netTxBps": sum(n["vm"]["netTxBps"] for n in on),
            "diskBps": sum(n["vm"]["diskReadBps"] + n["vm"]["diskWriteBps"] for n in on),
            "version": h.get("version", ""), "build": h.get("build", ""), "licence": h.get("licence", ""),
            "uptimeS": h.get("uptimeS"), "model": " ".join(x for x in (h.get("vendor", ""), h.get("model", "")) if x),
            "cpuModel": h.get("cpuModel", ""),
            "topCpu": top("cpuMhz"), "topMem": top("memActiveMB"), "topMemAllocated": top("memMB"),
            "datastores": [{"id": d["id"], "name": d["name"], **{k: d["ds"][k] for k in ("capacity", "used", "free", "usedRatio",
                                                                                         "provisionedRatio")}} for d in dss],
            "watchedS": self.history.watched_s(self.now) if self.history else 0,
        }
        ready = sum(1 for x in hosts if x["ready"])
        graph = {
            "generatedAt": iso_now(),
            "cluster": {"name": estate["name"], "openshift": False, "nodes": len(hosts), "product": self.snap["about"].get("fullName", "")},
            "sources": self.sources, "hotspots": N.hotspots(self.nodes, edges), "paths": paths, "cascades": [],
            "findings": findings, "changes": self.history.changes(self.now, self.vm_id, self.nodes) if self.history else [],
            "platform": {"components": [], "nodes": {"total": len(hosts), "ready": ready, "pressure": [], "cordoned": 0}, "cli": ""},
            "namespaces": namespaces, "externalHosts": [], "nodes": nodes, "edges": edges, "estate": estate,
            "categories": [{"id": c, "title": t} for c, t in CATEGORIES]}
        graph.update({k: v for k, v in summarize(findings).items() if k != "findings"})
        return graph


def _real(text: str) -> str:
    """Hardware names as the board reports them, minus the placeholders self-built machines carry."""
    t = (text or "").strip()
    return "" if t.lower() in ("system manufacturer", "system product name", "to be filled by o.e.m.", "default string",
                               "not specified", "none") else t


def _filesystems(disks: list[dict]) -> list[dict]:
    """One row per filesystem inside the guest. The same filesystem mounted in many places (container
    volumes, bind mounts) is reported once per mount; keep the shortest path of each."""
    seen: dict[tuple, dict] = {}
    for g in sorted(disks, key=lambda g: (len(g["path"]), g["path"])):
        seen.setdefault((g["capacity"], g["free"]), g)
    return list(seen.values())[:12]


def _rate(bytes_per_sec: float, *, disk: bool = False) -> str:
    """Disk rates read as bytes per second, network rates as bits per second."""
    b = float(bytes_per_sec or 0)
    if disk:
        for unit, size in (("GB/s", 1e9), ("MB/s", 1e6), ("kB/s", 1e3)):
            if b >= size:
                return f"{b / size:.1f} {unit}"
        return f"{math.floor(b)} B/s"
    b *= 8
    for unit, size in (("Gbps", 1e9), ("Mbps", 1e6), ("kbps", 1e3)):
        if b >= size:
            return f"{b / size:.1f} {unit}"
    return f"{b:.0f} bps"
