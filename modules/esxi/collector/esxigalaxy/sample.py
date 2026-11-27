# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""A made-up ESXi host for trying the module without one (ESXIGALAXY_MODE=sample) and for the tests.

`SampleHost.snapshot()` returns the same shape as `vsphere.Vsphere.snapshot()`, with one of each
kind of trouble the checks know about. Names and addresses are invented (documentation ranges);
nothing here describes a real host.
"""
from __future__ import annotations

import math
import zlib

GB = 2**30
DAY = 86400

# name, family, guest OS, power, vCPU, memory MB, active memory MB, CPU % of its vCPUs, disks [(GB, datastore, thin)],
# port groups, used GB (None = fully allocated), traits
VMS = [
    ("web-01", "linux", "Ubuntu Linux (64-bit)", True, 2, 4096, 900, 12, [(40, "ssd-01", True)], ["VM Network"], 22, {}),
    ("web-02", "linux", "Ubuntu Linux (64-bit)", True, 2, 4096, 850, 10, [(40, "ssd-01", True)], ["VM Network"], 21, {"iso": True}),
    ("proxy-01", "linux", "Debian GNU/Linux 12 (64-bit)", True, 2, 2048, 420, 6, [(20, "ssd-01", True)], ["DMZ"], 9, {}),
    ("db-01", "linux", "Ubuntu Linux (64-bit)", True, 8, 32768, 21000, 30, [(80, "ssd-01", False), (400, "bulk-01", False)],
     ["VM Network"], None, {"snapshots": [("before-upgrade", 41, 0), ("before-index-rebuild", 12, 1)], "snapshot_gb": 96, "ready_ms": 2400}),
    ("cache-01", "linux", "Ubuntu Linux (64-bit)", True, 4, 16384, 600, 3, [(30, "ssd-01", True)], ["VM Network"], 8, {}),
    ("build-01", "linux", "Ubuntu Linux (64-bit)", True, 6, 16384, 9000, 52, [(120, "ssd-01", True)], ["VM Network"], 88,
     {"ready_ms": 8400, "guest_full": True}),
    ("mail-01", "linux", "Debian GNU/Linux 11 (64-bit)", True, 2, 4096, 2900, 9, [(60, "bulk-01", True)], ["DMZ"], 41,
     {"balloon_mb": 900, "swapped_mb": 310, "swap_kbps": 140}),
    ("dc-01", "windows", "Microsoft Windows Server 2019 (64-bit)", True, 2, 8192, 3100, 7, [(80, "ssd-01", False)], ["VM Network"], None,
     {"tools": ("guestToolsNeedUpgrade", "guestToolsRunning")}),
    ("files-01", "windows", "Microsoft Windows Server 2022 (64-bit)", True, 4, 8192, 2400, 5,
     [(60, "ssd-01", False), (900, "bulk-01", True)], ["VM Network"], 780, {}),
    ("legacy-app", "windows", "Microsoft Windows Server 2012 (64-bit)", True, 2, 8192, 1900, 4, [(100, "bulk-01", False)],
     ["VM Network"], None, {"tools": ("guestToolsNotInstalled", "guestToolsNotRunning"), "consolidate": True}),
    ("monitor-01", "linux", "Ubuntu Linux (64-bit)", True, 2, 4096, 1500, 14, [(50, "ssd-01", True)], ["VM Network", "Lab trunk"], 31,
     {"nic_down": True}),
    ("lab-router", "unix", "FreeBSD 13 (64-bit)", True, 1, 1024, 300, 5, [(8, "ssd-01", True)], ["VM Network", "Lab trunk"], 3, {}),
    ("win10-test", "windows", "Microsoft Windows 10 (64-bit)", False, 4, 8192, 0, 0, [(120, "bulk-01", False)], ["Lab trunk"], None, {}),
    ("old-wiki", "linux", "CentOS 7 (64-bit)", False, 2, 4096, 0, 0, [(60, "bulk-01", False)], ["VM Network"], None,
     {"snapshots": [("pre-migration", 380, 0)], "snapshot_gb": 14}),
    ("template-ubuntu", "linux", "Ubuntu Linux (64-bit)", False, 2, 2048, 0, 0, [(20, "ssd-01", True)], ["VM Network"], 6, {"template": True}),
]
DATASTORES = [("ssd-01", "VMFS", 930), ("bulk-01", "VMFS", 1860)]
BULK_OTHER_GB = 180          # ISO images and old exports also sitting on bulk-01


def _wave(key: str, now: float, period: float = 90.0) -> float:
    """0.75..1.25, different for each key: keeps the sample moving."""
    phase = (zlib.crc32(key.encode()) % 1000) / 1000 * math.tau
    return 1.0 + 0.25 * math.sin(now / period * math.tau + phase)


class SampleHost:
    def __init__(self, started: float | None = None):
        self.started = started

    def snapshot(self, now: float) -> dict:
        if self.started is None:
            self.started = now
        vms, used = [], {name: 0.0 for name, _t, _c in DATASTORES}
        for i, (name, family, os_name, on, cpus, mem, active, cpu_pct, disks, nets, used_gb, t) in enumerate(VMS):
            wave = _wave(name, now)
            provisioned = sum(g for g, _d, _thin in disks) * GB
            committed = (used_gb * GB if used_gb is not None else provisioned) + t.get("snapshot_gb", 0) * GB
            share = {d: g / sum(x for x, _d, _t in disks) for g, d, _t in disks}
            for d, frac in share.items():
                used[d] += committed * frac
            tools = t.get("tools", ("guestToolsUnmanaged" if family != "windows" else "guestToolsCurrent", "guestToolsRunning"))
            snaps = [{"name": n, "description": "", "created": now - days * DAY, "depth": depth, "memory": False, "quiesced": False}
                     for n, days, depth in t.get("snapshots", [])]
            usage_mhz = cpus * 2200 * cpu_pct / 100 * wave if on else 0
            ip = f"192.0.2.{20 + i}"
            vms.append({
                "id": f"vm-{100 + i}", "name": name, "uuid": f"4203{i:04d}-0000-4000-8000-00000000{i:04d}", "template": bool(t.get("template")),
                "annotation": "", "powerState": "poweredOn" if on else "poweredOff", "connectionState": "connected",
                "overallStatus": "green", "guestId": "", "guestFullName": os_name, "family": family,
                "hostname": name if on and tools[1] == "guestToolsRunning" else "",
                "ips": [ip] if on and tools[1] == "guestToolsRunning" else [],
                "tools": {"status": tools[0], "running": tools[1] if on else "guestToolsNotRunning", "version": "12352"},
                "guestDisks": ([{"path": "/", "capacity": disks[0][0] * GB, "free": int(disks[0][0] * GB * (0.04 if t.get("guest_full") else 0.45))}]
                               if on and tools[1] == "guestToolsRunning" else []),
                "numCpu": cpus, "coresPerSocket": 1, "memMB": mem, "cpuReservation": 0, "memReservationMB": 0, "cpuLimit": -1,
                "memLimitMB": -1, "hwVersion": "vmx-13" if name == "legacy-app" else "vmx-19", "firmware": "efi",
                "changed": now - (3 + i) * DAY, "bootTime": now - (5 + i * 3) * DAY if on else None,
                "uptimeS": (5 + i * 3) * DAY if on else 0, "consolidationNeeded": bool(t.get("consolidate")), "question": "",
                "nics": [{"label": f"Network adapter {k + 1}", "mac": f"00:50:56:00:{i:02x}:{k:02x}", "network": net, "type": "Vmxnet3",
                          "connected": on and not (t.get("nic_down") and k == 1), "startConnected": True} for k, net in enumerate(nets)],
                "disks": [{"label": f"Hard disk {k + 1}", "capacity": g * GB, "datastore": d, "file": f"[{d}] {name}/{name}_{k}.vmdk",
                           "thin": thin, "mode": "persistent", "delta": bool(snaps)} for k, (g, d, thin) in enumerate(disks)],
                "cdroms": [{"label": "CD/DVD drive 1", "connected": bool(t.get("iso")) and on, "startConnected": bool(t.get("iso")),
                            "iso": "[bulk-01] iso/ubuntu-24.04-live-server-amd64.iso" if t.get("iso") else ""}],
                "datastores": sorted({d for _g, d, _t in disks}), "networks": sorted(set(nets)), "resourcePool": "ha-root-pool",
                "committed": int(committed), "uncommitted": int(max(0, provisioned - committed)), "provisioned": int(provisioned),
                "quick": {"cpuMhz": round(usage_mhz), "cpuDemandMhz": round(usage_mhz * 1.1),
                          "hostMemMB": round(mem * 0.97 - t.get("balloon_mb", 0) - t.get("swapped_mb", 0)) if on else 0,
                          "guestMemMB": round(active * wave) if on else 0, "balloonMB": t.get("balloon_mb", 0) if on else 0,
                          "swappedMB": t.get("swapped_mb", 0) if on else 0, "compressedMB": 0, "sharedMB": 0,
                          "overheadMB": 40 + mem // 256 if on else 0},
                "maxCpuMhz": cpus * 2200, "snapshots": snaps, "snapshotBytes": t.get("snapshot_gb", 0) * GB,
                "perf": {"cpuUsagePct": cpu_pct * 100 * wave, "cpuReadyMs": t.get("ready_ms", 60 * cpus) * wave,
                         "memActiveKb": active * 1024 * wave, "swapInKbps": t.get("swap_kbps", 0),
                         "netRxKbps": (40 + 30 * cpu_pct) * wave, "netTxKbps": (25 + 18 * cpu_pct) * _wave(name + "tx", now),
                         "diskReadKbps": (12 * cpu_pct) * wave, "diskWriteKbps": (30 + 22 * cpu_pct) * _wave(name + "w", now),
                         "diskLatencyMs": 31 if name == "mail-01" else 2} if on else {},
            })
        used["bulk-01"] += BULK_OTHER_GB * GB
        datastores = [{
            "id": f"ds-{i}", "name": name, "type": kind, "accessible": True, "url": f"/vmfs/volumes/{name}", "capacity": cap * GB,
            "free": int(max(0, cap * GB - used[name])), "maintenance": "normal",
            "uncommitted": int(sum(v["uncommitted"] for v in vms if name in v["datastores"])),
            "vms": sum(1 for v in vms if name in v["datastores"])} for i, (name, kind, cap) in enumerate(DATASTORES)]
        on = [v for v in vms if v["powerState"] == "poweredOn"]
        mem_bytes = 116 * GB
        mem_used = sum((v["quick"]["hostMemMB"] + v["quick"]["overheadMB"]) * 2**20 for v in on) + 4 * GB
        loose = {"promiscuous": False, "macChanges": False, "forgedTransmits": False}
        host = {
            "id": "ha-host", "name": "esx-lab-01.example.net", "vendor": "Supermicro", "model": "SYS-5019P", "serial": "S000000X0000000",
            "cpuModel": "Intel(R) Xeon(R) Silver 4214 CPU @ 2.20GHz", "sockets": 1, "cores": 12, "threads": 24, "cpuMhz": 2200,
            "hz": 2_200_000_000, "memBytes": mem_bytes, "cpuUsedMhz": round(sum(v["quick"]["cpuMhz"] for v in on) + 600),
            "memUsedBytes": int(min(mem_used, mem_bytes * 0.985)), "uptimeS": 143 * DAY, "bootTime": now - 143 * DAY,
            "connectionState": "connected", "powerState": "poweredOn", "maintenance": False, "overallStatus": "yellow",
            "product": {"name": "VMware ESXi", "version": "7.0.3", "build": "21930508", "fullName": "VMware ESXi 7.0.3 build-21930508"},
            "managementIp": "", "lockdown": "lockdownDisabled", "clockSkewS": 412.0,
            "ntp": {"servers": [], "running": False, "policy": "off"},
            "services": [{"key": "DCUI", "label": "Direct Console UI", "running": True, "policy": "on"},
                         {"key": "TSM", "label": "ESXi Shell", "running": False, "policy": "off"},
                         {"key": "TSM-SSH", "label": "SSH", "running": True, "policy": "on"},
                         {"key": "ntpd", "label": "NTP Daemon", "running": False, "policy": "off"},
                         {"key": "vmsyslogd", "label": "Syslog Server", "running": True, "policy": "on"}],
            "firewall": {"rulesets": [
                {"key": "sshServer", "label": "SSH Server", "enabled": True, "required": False, "allIp": True, "allowed": [],
                 "rules": [{"port": 22, "endPort": None, "direction": "inbound", "protocol": "tcp"}]},
                {"key": "vSphereClient", "label": "vSphere Web Client", "enabled": True, "required": True, "allIp": True, "allowed": [],
                 "rules": [{"port": 443, "endPort": None, "direction": "inbound", "protocol": "tcp"}]},
                {"key": "ntpClient", "label": "NTP Client", "enabled": False, "required": False, "allIp": True, "allowed": [],
                 "rules": [{"port": 123, "endPort": None, "direction": "outbound", "protocol": "udp"}]},
                {"key": "snmp", "label": "SNMP Server", "enabled": True, "required": False, "allIp": False, "allowed": ["192.0.2.5"],
                 "rules": [{"port": 161, "endPort": None, "direction": "inbound", "protocol": "udp"}]}]},
            "sensors": [{"name": "System Board 1 Fan 2", "type": "fan", "state": "yellow", "reading": 900, "unit": "RPM"},
                        {"name": "CPU 1 Temp", "type": "temperature", "state": "green", "reading": 54, "unit": "degrees C"}],
            "vswitches": [
                {"name": "vSwitch0", "ports": 1536, "portsFree": 1510, "mtu": 1500, "uplinks": ["vmnic0", "vmnic1"], "security": dict(loose),
                 "portgroups": ["VM Network", "Management Network"]},
                {"name": "vSwitch1", "ports": 1536, "portsFree": 1528, "mtu": 1500, "uplinks": ["vmnic2"], "security": dict(loose),
                 "portgroups": ["DMZ", "Lab trunk"]}],
            "portgroups": [
                {"name": "VM Network", "vswitch": "vSwitch0", "vlan": 0, "security": dict(loose), "kernel": False},
                {"name": "Management Network", "vswitch": "vSwitch0", "vlan": 0, "security": dict(loose), "kernel": True},
                {"name": "DMZ", "vswitch": "vSwitch1", "vlan": 20, "security": dict(loose), "kernel": False},
                {"name": "Lab trunk", "vswitch": "vSwitch1", "vlan": 4095,
                 "security": {"promiscuous": True, "macChanges": True, "forgedTransmits": True}, "kernel": False}],
            "pnics": [{"device": "vmnic0", "mac": "ac:1f:6b:00:00:10", "driver": "ixgben", "linkUp": True, "speedMb": 10000, "duplex": True},
                      {"device": "vmnic1", "mac": "ac:1f:6b:00:00:11", "driver": "ixgben", "linkUp": True, "speedMb": 10000, "duplex": True},
                      {"device": "vmnic2", "mac": "ac:1f:6b:00:00:12", "driver": "igbn", "linkUp": True, "speedMb": 100, "duplex": True},
                      {"device": "vmnic3", "mac": "ac:1f:6b:00:00:13", "driver": "igbn", "linkUp": False, "speedMb": 0, "duplex": False}],
            "vmknics": [{"device": "vmk0", "portgroup": "Management Network", "ip": "192.0.2.10", "mac": "ac:1f:6b:00:00:10"}],
            "perf": {"cpuUsagePct": 6200.0, "netRxKbps": sum(v["perf"].get("netRxKbps", 0) for v in on),
                     "netTxKbps": sum(v["perf"].get("netTxKbps", 0) for v in on), "diskReadKbps": 900.0, "diskWriteKbps": 2600.0,
                     "diskLatencyMs": 31, "swapInKbps": 140.0},
        }
        return {
            "at": now,
            "about": {"fullName": "VMware ESXi 7.0.3 build-21930508", "version": "7.0.3", "build": "21930508", "apiType": "HostAgent",
                      "apiVersion": "7.0.3.0", "licence": "vSphere 7 Standard", "licenceEdition": "esx.standard.cpuPackage", "vcenter": False},
            "hosts": [host], "datastores": datastores, "vms": vms, "warnings": [],
            "tasks": [{"name": "VirtualMachine.createSnapshot", "entity": "db-01", "state": "success", "user": "root",
                       "queued": now - 12 * DAY, "completed": now - 12 * DAY + 40, "error": ""},
                      {"name": "VirtualMachine.powerOff", "entity": "win10-test", "state": "success", "user": "root",
                       "queued": now - 3600 * 5, "completed": now - 3600 * 5 + 6, "error": ""}],
        }
