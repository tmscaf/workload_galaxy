# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""What looks wrong on the host, why it matters, and what to do about it.

Each finding names one subject (a VM, a datastore, a port group, the host), says in plain words
what was seen, and offers a next step. Nothing here changes the host: the module only reads.

Categories, in the order someone checking a hypervisor usually wants them:
    capacity      the host is running out of memory or cores for what it has been asked to run
    performance   a VM is waiting: for a physical core, for memory, for its disk
    storage       datastores filling up, snapshots, space held by things that are switched off
    vm            a VM's own setup: VMware Tools, adapters, media left attached
    network       virtual switches and the physical cards behind them
    security      how the host itself is reachable and locked down
    sizing        VMs given far more than they use (information, with what to change)
    platform      the hypervisor itself: clock, version, hardware health, and this monitoring
"""
from __future__ import annotations

import logging
import math

log = logging.getLogger(__name__)
GB = 2**30
MB = 2**20
DAY = 86400

CATEGORIES = [("capacity", "Capacity"), ("performance", "Performance"), ("storage", "Storage & snapshots"), ("vm", "VM setup"),
              ("network", "Networking"), ("security", "Host security"), ("sizing", "Right-sizing"), ("platform", "Platform")]
PATTERN_LABELS = {
    "storage:poweredoff": "powered-off VMs still holding disk space", "storage:snapshot": "VMs carrying snapshots",
    "sizing:mem": "VMs allocated far more memory than they use", "sizing:cpu": "VMs with more vCPUs than they use",
    "vm:iso": "VMs with an ISO image left attached", "vm:tools-missing": "running VMs without VMware Tools",
    "vm:tools-old": "VMs with out-of-date VMware Tools", "vm:tools-stopped": "VMs whose VMware Tools are not running",
    "performance:ready": "VMs waiting for a physical core", "capacity:balloon": "VMs having memory taken back by the host",
    "storage:guestdisk": "VMs with a nearly full disk inside the guest", "vm:nic": "running VMs with a disconnected network adapter",
}
# End of general support, from VMware's product lifecycle: after it there are no more regular patches.
END_OF_SUPPORT = {"6.0": (2020, 3, 12), "6.5": (2022, 10, 15), "6.7": (2022, 10, 15), "7.0": (2025, 10, 2), "8.0": (2027, 10, 11)}
MIN_WATCH_S = 3600          # sizing advice waits until a VM has been watched this long


def _gb(n: float | None) -> str:
    from .graph import gb
    return gb(n)


def _days(d: float) -> str:
    d = float(d)
    if d < 1:
        return f"{max(1, round(d * 24))} h"
    if d < 60:
        return f"{round(d)} day{'s' if round(d) != 1 else ''}"
    if d < 730:
        return f"{round(d / 30)} months"
    return f"{d / 365:.1f} years"


def _small_pct(x: float) -> str:
    return "under 1%" if x < 0.01 else f"{x * 100:.0f}%"


def suggest_memory_mb(allocated_mb: int, peak_active_mb: float) -> int | None:
    """A smaller allocation worth proposing: four times the most it has been actively using, never under
    2 GB, and only when that at least halves it. Deliberately cautious: the host's "active" figure is a
    sampled estimate of recently touched memory and reads low for anything that sits on a large cache."""
    target = max(2048, math.ceil(peak_active_mb * 4 / 1024) * 1024)
    return target if target * 2 <= allocated_mb else None


def suggest_vcpus(vcpus: int, peak_pct: float) -> int | None:
    """Fewer vCPUs that would still leave the busiest moment under about 60%."""
    need = max(1, math.ceil(vcpus * peak_pct / 0.6))
    target = max(1, need if need < 2 else 2 ** math.ceil(math.log2(need)))
    return target if target * 2 <= vcpus else None


class Checks:
    def __init__(self, builder):
        self.b = builder
        self.s = builder.s
        self.add = builder.findings.add
        self.now = builder.now
        self.vms = [n for n in builder.nodes.values() if n["kind"] == "workload"]
        self.on = [n for n in self.vms if n["vm"]["on"]]
        self.hosts = [n for n in builder.nodes.values() if n["kind"] == "host"]

    def run(self) -> None:
        for step in (self._capacity, self._performance, self._datastores, self._snapshots, self._vm_setup, self._network,
                     self._security, self._sizing, self._platform, self._monitoring):
            try:
                step()
            except Exception:  # noqa: BLE001 - one broken check must never take the map down
                log.exception("check %s failed", step.__name__)

    # ------------------------------------------------------------------ what could be handed back
    def _reclaimable(self) -> list[tuple[dict, int, int]]:
        """[(vm node, suggested MB, MB freed)], biggest saving first. Only VMs watched long enough."""
        out = []
        for n in self.on:
            vm = n["vm"]
            if vm["watchedS"] < MIN_WATCH_S or vm["peakMemActiveMB"] is None:
                continue
            target = suggest_memory_mb(vm["memMB"], vm["peakMemActiveMB"])
            if target:
                out.append((n, target, vm["memMB"] - target))
        return sorted(out, key=lambda x: -x[2])

    def _watch_note(self) -> str:
        w = self.b.history.watched_s(self.now) if self.b.history else 0
        if w < MIN_WATCH_S:
            return "This module has only just started watching; sizing suggestions appear after an hour."
        return (f"Sized from the busiest each VM has been in the {_days(min(w, 14 * DAY) / DAY)} this module has watched. "
                "The host estimates active memory by sampling and it leaves out file cache, so check inside the guest "
                "(free -m, Task Manager) before cutting.")

    # ------------------------------------------------------------------ capacity
    def _capacity(self) -> None:
        for h in self.hosts:
            e = h["esx"]
            used, total = e["memUsedBytes"], e["memBytes"] or 1
            ratio = used / total
            pressure = [n for n in self.on if n["vm"]["balloonMB"] or n["vm"]["swappedMB"]]
            if ratio * 100 >= self.s.memory_warn_pct:
                tight = ratio >= 0.97 or bool(pressure)
                rec = self._reclaimable()
                opts = []
                if rec:
                    freed = sum(f for _n, _t, f in rec[:6])
                    lines = "; ".join(f"{n['name']} {_gb(n['vm']['memMB'] * MB)} → {_gb(t * MB)}" for n, t, _f in rec[:6])
                    opts.append({"title": f"Take back about {_gb(freed * MB)} from VMs that are not using it",
                                 "detail": lines + ".",
                                 "note": "Each needs a shutdown to change (Edit settings > Memory), unless memory hot-plug is on."})
                idle = sorted((n for n in self.on if n["vm"]["cpuPct"] < 0.02 and n["vm"]["netRxBps"] + n["vm"]["netTxBps"] < 20000),
                              key=lambda n: -n["vm"]["memMB"])[:5]
                if idle:
                    opts.append({"title": "Shut down VMs that are doing nothing",
                                 "detail": "Nearly idle right now: " + ", ".join(f"{n['name']} ({_gb(n['vm']['memMB'] * MB)})" for n in idle) + ".",
                                 "note": "Idle at this moment is not the same as unused: check before switching anything off."})
                opts.append({"title": "Add memory to the host",
                             "detail": f"It has {_gb(total)}; running VMs are allocated {_gb(e['memAllocated'])}."})
                self.add("capacity", f"memory:{h['id']}", f"{h['name']} is using {ratio * 100:.0f}% of its memory",
                         subject=h["id"], weight=0.7 if tight else 0.45,
                         detail=f"{_gb(used)} of {_gb(total)} is in use, leaving {_gb(total - used)}. Running VMs are allocated "
                                f"{_gb(e['memAllocated'])} in total." + (f" {len(pressure)} VM{'s are' if len(pressure) != 1 else ' is'} "
                                                                         "already having memory taken back." if pressure else ""),
                         cause="When a host runs out of free memory it takes memory back from VMs (ballooning), then swaps their "
                               "memory to disk, which slows them badly. A VM that is powered on now may also fail to start again "
                               "after a reboot if there is no room for it.",
                         fix="Free memory before starting anything else on this host.",
                         evidence=[f"memory in use: {_gb(used)} of {_gb(total)}", f"allocated to running VMs: {_gb(e['memAllocated'])}",
                                   f"allocated to powered-off VMs: {_gb(e['memAllocatedAll'] - e['memAllocated'])}"],
                         options=opts, options_note=self._watch_note())
            elif e["memAllocRatio"] and e["memAllocRatio"] > 1.0:
                self.add("capacity", f"overcommit:{h['id']}",
                         f"{h['name']} has promised its VMs more memory than it has ({e['memAllocRatio']:.2f}×)",
                         subject=h["id"], severity="warning" if e["memAllocRatio"] >= 1.5 else "info",
                         weight=0.3 if e["memAllocRatio"] >= 1.5 else 0,
                         detail=f"Running VMs are allocated {_gb(e['memAllocated'])}; the host has {_gb(total)} and is using {_gb(used)}.",
                         cause="This works while VMs leave part of their memory untouched. If they all use what they were given, "
                               "the host has to balloon and swap.",
                         fix="Nothing to do while usage stays well under the total; avoid adding more.")
            if e["vcpuRatio"] and e["vcpuRatio"] >= 3:
                waiting = [n for n in self.on if (n["vm"]["readyPct"] or 0) * 100 >= self.s.ready_warn_pct]
                heavy = e["vcpuRatio"] >= 6 or bool(waiting)
                self.add("capacity", f"vcpu:{h['id']}",
                         f"{h['name']} runs {e['vcpus']} vCPUs on {e['cores']} cores ({e['vcpuRatio']:.1f} to 1)",
                         subject=h["id"], severity="warning" if heavy else "info", weight=0.3 if heavy else 0,
                         detail=f"{len(self.on)} running VMs share {e['cores']} physical cores ({e['threads']} threads). "
                                f"The host is at {e['cpuPct'] * 100:.0f}% CPU.",
                         cause="Every vCPU has to be scheduled onto a physical core. The more vCPUs per core, the longer VMs wait "
                               "for one when several are busy at once, even if the host as a whole is not full.",
                         fix="Fine while VMs are not waiting (see Performance). Give VMs only the vCPUs they use.")
        for n in self.on:
            vm = n["vm"]
            if vm["swappedMB"] or vm["balloonMB"]:
                swapping = vm["swappedMB"] > 0
                self.add("capacity", f"balloon:{n['id']}",
                         f"{n['name']} is having memory taken back by the host"
                         + (f" ({_gb(vm['swappedMB'] * MB)} swapped to disk)" if swapping else f" ({_gb(vm['balloonMB'] * MB)} ballooned)"),
                         subject=n["id"], weight=(0.6 if vm["swapInKbps"] else 0.5) if swapping else 0.35,
                         detail=f"Ballooned: {_gb(vm['balloonMB'] * MB)}. Swapped by the host: {_gb(vm['swappedMB'] * MB)}. "
                                f"The VM is allocated {_gb(vm['memMB'] * MB)}.",
                         cause="The host ran short of memory. Ballooning asks the guest to give memory up, which is mild. "
                               "Host swapping moves the VM's memory to disk without the guest knowing, and every read back is slow.",
                         fix="Free memory on the host (see Capacity); the swapped memory comes back only as the VM touches it or is restarted.",
                         evidence=[f"swap-in rate: {vm['swapInKbps']:.0f} kB/s" if vm["swapInKbps"] else ""])

    # ------------------------------------------------------------------ performance
    def _performance(self) -> None:
        busy = sorted(self.on, key=lambda n: -n["vm"]["cpuMhz"])
        for n in self.on:
            vm = n["vm"]
            ready = (vm["readyPct"] or 0) * 100
            if ready >= self.s.ready_warn_pct:
                others = [x for x in busy if x["id"] != n["id"]][:3]
                opts = []
                peak = vm["peakCpuPct"] if vm["peakCpuPct"] is not None else vm["cpuPct"]
                fewer = suggest_vcpus(vm["cpus"], peak) if vm["watchedS"] >= MIN_WATCH_S else None
                if fewer:
                    opts.append({"title": f"Give {n['name']} {fewer} vCPU{'s' if fewer != 1 else ''} instead of {vm['cpus']}",
                                 "detail": f"It has peaked at {peak * 100:.0f}% of its {vm['cpus']} vCPUs. A VM with fewer vCPUs is "
                                           "easier to schedule and waits less.", "note": "needs a shutdown"})
                if others:
                    opts.append({"title": "Look at what it is competing with",
                                 "detail": "Busiest VMs on the host now: " + ", ".join(f"{x['name']} ({x['vm']['cpuPct'] * 100:.0f}% of "
                                                                                       f"{x['vm']['cpus']} vCPUs)" for x in others) + "."})
                self.add("performance", f"ready:{n['id']}",
                         f"{n['name']} spends {ready:.0f}% of its time waiting for a physical core",
                         subject=n["id"], weight=0.65 if ready >= 2 * self.s.ready_warn_pct else 0.4,
                         detail=f"CPU ready time is {ready:.1f}% per vCPU over the last few minutes ({vm['cpus']} vCPUs, using "
                                f"{vm['cpuPct'] * 100:.0f}% of them).",
                         cause="The VM had work to do but no physical core was free. It shows up inside the guest as slowness "
                               "that the guest's own CPU graphs do not explain.",
                         fix="Reduce vCPUs on this VM or its neighbours, or move load off the host.", options=opts)
            lat = vm["diskLatencyMs"]
            if lat is not None and lat >= 20:
                self.add("performance", f"latency:{n['id']}", f"{n['name']} is waiting up to {lat:.0f} ms for its disk",
                         subject=n["id"], weight=0.6 if lat >= 50 else 0.35,
                         detail=f"Highest disk latency in the last few minutes: {lat:.0f} ms. On {', '.join(vm['datastores']) or 'its datastore'}.",
                         cause="Above about 20 ms, applications feel it. Usual causes: a busy neighbour on the same datastore, a "
                               "snapshot chain, a failing or rebuilding disk, or the host swapping.",
                         fix="Check what else is writing to the same datastore, and whether this VM carries snapshots.")
        for h in self.hosts:
            if h["esx"]["cpuPct"] >= 0.85:
                self.add("performance", f"cpu:{h['id']}", f"{h['name']} is at {h['esx']['cpuPct'] * 100:.0f}% CPU", subject=h["id"],
                         weight=0.45, detail="Busiest: " + ", ".join(f"{x['name']} ({x['vm']['cpuMhz']} MHz)" for x in busy[:4]) + ".",
                         cause="With the host this busy, every VM waits longer for a core.", fix="Move or slow down the busiest VMs.")

    # ------------------------------------------------------------------ datastores
    def _datastores(self) -> None:
        for n in [x for x in self.b.nodes.values() if x.get("ds")]:
            d = n["ds"]
            if not d["accessible"]:
                self.add("storage", f"inaccessible:{n['id']}", f"Datastore {n['name']} is not accessible", subject=n["id"], weight=0.9,
                         detail="The host cannot read or write this datastore.",
                         cause="Every VM with a disk on it is stopped or about to hang.",
                         fix="Check the storage device or the path to it (Storage > Devices in the Host Client).")
                continue
            used = d["usedRatio"] * 100
            on_it = [v for v in self.vms if n["name"] in v["vm"]["datastores"]]
            if used >= self.s.datastore_warn_pct:
                snaps = sorted((v for v in on_it if v["vm"]["snapshotBytes"]), key=lambda v: -v["vm"]["snapshotBytes"])[:4]
                off = sorted((v for v in on_it if not v["vm"]["on"]), key=lambda v: -v["vm"]["committed"])[:4]
                opts = []
                if snaps:
                    opts.append({"title": f"Delete snapshots (about {_gb(sum(v['vm']['snapshotBytes'] for v in snaps))})",
                                 "detail": ", ".join(f"{v['name']} {_gb(v['vm']['snapshotBytes'])}" for v in snaps) + ".",
                                 "note": "Deleting a snapshot merges it into the disk: it needs some free space and takes a while on a busy VM."})
                if off:
                    opts.append({"title": f"Remove or archive powered-off VMs (about {_gb(sum(v['vm']['committed'] for v in off))})",
                                 "detail": ", ".join(f"{v['name']} {_gb(v['vm']['committed'])}" for v in off) + ".",
                                 "note": "Export first (OVF) if there is any chance it is needed again."})
                opts.append({"title": "Add capacity", "detail": "Extend the datastore or move VMs to another one."})
                self.add("storage", f"full:{n['id']}", f"Datastore {n['name']} is {used:.0f}% full ({_gb(d['free'])} left)",
                         subject=n["id"], weight=0.75 if used >= self.s.datastore_crit_pct else 0.45,
                         detail=f"{_gb(d['used'])} of {_gb(d['capacity'])} is used by {len(on_it)} VMs. "
                                f"Thin disks could still grow by {_gb(max(0, d['provisioned'] - d['used']))}.",
                         cause="When a datastore fills, every VM with a thin disk or a snapshot on it is paused until space is freed.",
                         fix="Free space before it runs out.", options=opts)
            if d["provisionedRatio"] > 1.0:
                risky = d["provisionedRatio"] >= 1.2 and used >= 70
                self.add("storage", f"overprov:{n['id']}",
                         f"Datastore {n['name']} has promised {_gb(d['provisioned'])} on {_gb(d['capacity'])} of disk",
                         subject=n["id"], severity="warning" if risky else "info", weight=0.3 if risky else 0,
                         detail=f"Thin disks on it are allowed to grow to {d['provisionedRatio'] * 100:.0f}% of its capacity. "
                                f"It is {used:.0f}% full now.",
                         cause="Thin disks take space only as they are written. If the VMs fill what they were promised, the "
                               "datastore runs out and they pause.",
                         fix="Watch the free space; keep the total within what you could live with being used.")
        for n in self.vms:
            vm = n["vm"]
            if not vm["on"] and vm["committed"] and not vm["template"]:
                self.add("storage", f"poweredoff:{n['id']}", f"{n['name']} is powered off and holds {_gb(vm['committed'])}",
                         subject=n["id"], severity="info", weight=0,
                         detail=f"On {', '.join(vm['datastores']) or 'the datastore'}. Allocated {vm['cpus']} vCPUs and {_gb(vm['memMB'] * MB)} "
                                "of memory if started.",
                         cause="A VM that is off uses no memory or CPU, but its disks stay on the datastore.",
                         fix="Keep it if it is needed; otherwise export and delete it to get the space back.")
            for g in vm["guestDisks"]:
                if g["capacity"] > GB and g["free"] / g["capacity"] < 0.10:
                    free = g["free"] / g["capacity"]
                    self.add("storage", f"guestdisk:{n['id']}:{g['path']}",
                             f"{n['name']}: {g['path']} is {100 - free * 100:.0f}% full inside the guest",
                             subject=n["id"], weight=0.6 if free < 0.05 else 0.4,
                             detail=f"{_gb(g['free'])} free of {_gb(g['capacity'])}, as VMware Tools reports it.",
                             cause="A full filesystem inside the guest stops databases, logging and updates, whatever the datastore has left.",
                             fix="Clean up inside the guest, or grow the virtual disk and then the filesystem.")

    def _snapshots(self) -> None:
        for n in self.vms:
            vm = n["vm"]
            if vm["consolidationNeeded"]:
                self.add("storage", f"consolidate:{n['id']}", f"{n['name']} needs its disks consolidated", subject=n["id"], weight=0.4,
                         detail="A snapshot was deleted but its files were not merged back into the disk.",
                         cause="The VM keeps running on leftover snapshot files, which grow and slow its disk like a snapshot does.",
                         fix="Host Client: right-click the VM > Snapshots > Consolidate disks.")
            if not vm["snapshotCount"]:
                continue
            age = vm["snapshotOldestDays"] or 0
            old = age >= self.s.snapshot_days
            big = vm["snapshotBytes"] >= 50 * GB
            names = ", ".join(f"{s['name']} ({_days(s['ageDays'] or 0)})" for s in vm["snapshots"][:4])
            count = vm["snapshotCount"]
            holding = f", holding {_gb(vm['snapshotBytes'])}" if vm["snapshotBytes"] >= GB else ""
            title = (f"{n['name']} has a snapshot {_days(age)} old{holding}" if count == 1 else
                     f"{n['name']} has {count} snapshots, the oldest {_days(age)} old{holding}")
            self.add("storage", f"snapshot:{n['id']}", title,
                     subject=n["id"], severity=None if old or big else "info",
                     weight=(0.45 if (age >= 30 or big) else 0.3) if old or big else 0,
                     detail=f"Snapshots: {names}. Together they hold about {_gb(vm['snapshotBytes'])} beyond the VM's own disks.",
                     cause="A snapshot is not a backup: it is a growing file of every change since it was taken, on the same "
                           "datastore. The older it gets, the more space it takes, the slower the VM's disk, and the longer "
                           "it takes to delete.",
                     fix="Delete snapshots once the change they protected has proved itself.",
                     options=[{"title": "Delete all of them", "detail": "Host Client: right-click the VM > Snapshots > Manage snapshots > "
                               "Delete all. The VM keeps its current state; the snapshot files are merged into its disks.",
                               "note": "merging needs free space on the datastore and can take hours for a large, busy VM"},
                              {"title": "Keep the newest, delete the rest", "detail": "Delete from the oldest, one at a time, so each merge is small."}]
                     if old or big else None)

    # ------------------------------------------------------------------ a VM's own setup
    def _vm_setup(self) -> None:
        for n in self.vms:
            vm = n["vm"]
            if vm["question"]:
                self.add("vm", f"question:{n['id']}", f"{n['name']} is stopped, waiting for an answer", subject=n["id"], weight=0.8,
                         detail=vm["question"][:400], cause="The host paused the VM and will not continue until someone answers.",
                         fix="Answer it in the Host Client (the VM shows a yellow question banner).")
            for cd in vm["cdroms"]:
                if cd["iso"] and (cd["connected"] or cd["startConnected"]):
                    self.add("vm", f"iso:{n['id']}", f"{n['name']} still has an ISO image attached", subject=n["id"], severity="info",
                             weight=0, detail=f"{cd['label']}: {cd['iso']}",
                             cause="Usually left over from installing the OS. The VM can boot from it by accident, and the image "
                                   "cannot be deleted while it is attached.",
                             fix="Edit settings > CD/DVD drive: untick Connect and Connect at power on.")
                    break
            if not vm["on"]:
                continue
            status = vm["tools"]["status"]
            if status == "guestToolsNotInstalled":
                self.add("vm", f"tools-missing:{n['id']}", f"{n['name']} runs without VMware Tools", subject=n["id"], weight=0.25,
                         detail=f"Guest: {vm['os'] or 'unknown'}.",
                         cause="Without Tools the host cannot shut the guest down cleanly, cannot see its address, hostname or "
                               "disk usage, and cannot reclaim memory gently.",
                         fix="Install open-vm-tools (Linux) or VMware Tools (Windows) in the guest.")
            elif not vm["toolsRunning"]:
                self.add("vm", f"tools-stopped:{n['id']}", f"{n['name']}: VMware Tools is installed but not running", subject=n["id"],
                         weight=0.3, detail=f"Tools version status: {vm['toolsLabel']}.",
                         cause="A clean shutdown from the host will not work, and guest details (address, disks) are missing.",
                         fix="Start the service in the guest (vmtoolsd / open-vm-tools, or the VMware Tools service on Windows).")
            elif status in ("guestToolsNeedUpgrade", "guestToolsSupportedOld", "guestToolsTooOld"):
                self.add("vm", f"tools-old:{n['id']}", f"{n['name']}: VMware Tools is out of date", subject=n["id"], severity="info",
                         weight=0, detail=f"Version {vm['tools']['version'] or 'unknown'} ({vm['toolsLabel']}).",
                         cause="Old Tools miss driver and security fixes.", fix="Upgrade Tools in the guest at the next maintenance window.")
            down = [x for x in vm["nics"] if not x["connected"]]
            if down:
                self.add("vm", f"nic:{n['id']}", f"{n['name']} has a network adapter that is not connected", subject=n["id"], weight=0.3,
                         detail="; ".join(f"{x['label']} on {x['network'] or 'no network'}" for x in down),
                         cause="The adapter exists but is unplugged, so the guest sees a dead link on that network.",
                         fix="Edit settings > Network adapter: tick Connect (and Connect at power on), or remove the adapter.")

    # ------------------------------------------------------------------ networking
    def _network(self) -> None:
        for h in self.hosts:
            e = h["esx"]
            pnic = {p["device"]: p for p in e["pnics"]}
            used = {u for sw in e["vswitches"] for u in sw["uplinks"]}
            spare = [p for p in e["pnics"] if p["device"] not in used]
            for sw in e["vswitches"]:
                ups = [pnic[u] for u in sw["uplinks"] if u in pnic]
                live = [p for p in ups if p["linkUp"]]
                groups = ", ".join(sw["portgroups"][:5])
                for p in ups:
                    nid = self.b.nic_id.get(p["device"], "")
                    if not p["linkUp"]:
                        self.add("network", f"linkdown:{p['device']}", f"{p['device']} has no link ({sw['name']})", subject=nid,
                                 weight=0.8 if not live else 0.45,
                                 detail=f"It is an uplink of {sw['name']} ({groups})." + (" It is the only one: VMs on these networks "
                                                                                      "are cut off from everything outside the host." if not live else
                                                                                      " Traffic is using the remaining uplink."),
                                 cause="Cable unplugged, switch port down, or the card has failed.", fix="Check the cable and the switch port.")
                    elif p["speedMb"] < 1000 or not p["duplex"]:
                        self.add("network", f"speed:{p['device']}",
                                 f"{p['device']} linked at {p['speedMb']} Mbps{'' if p['duplex'] else ', half duplex'} ({sw['name']})",
                                 subject=nid, weight=0.45 if not p["duplex"] else 0.35,
                                 detail=f"Every VM on {groups} shares this link.",
                                 cause="A link slower than the card supports usually means a bad cable, a damaged port, or a switch "
                                       "port forced to a fixed speed.",
                                 fix="Replace the cable; check the switch port is set to auto-negotiate.")
                if len(ups) == 1 and live:
                    idle = [p["device"] for p in spare]
                    self.add("network", f"redundancy:{sw['name']}", f"{sw['name']} has a single uplink ({ups[0]['device']})",
                             subject=self.b.nic_id.get(ups[0]["device"], ""), severity="info", weight=0,
                             detail=f"Networks on it: {groups}." + (f" Other cards in the host: "
                                    + ", ".join(f"{p['device']} ({'link up' if p['linkUp'] else 'no link'})" for p in spare) + "." if idle else ""),
                             cause="One cable, switch port or card failing cuts every VM on these networks off, and the host's "
                                   "management address with them if it is on this switch.",
                             fix="Add a second uplink to the virtual switch (Networking > Virtual switches > Edit > Add uplink) "
                                 "if another card can be cabled.")
            for g in e["portgroups"]:
                nid = self.b.pg_id.get(g["name"], "")
                sec = g["security"]
                if sec["promiscuous"]:
                    self.add("network", f"promiscuous:{g['name']}", f"Port group {g['name']} lets VMs see each other's traffic",
                             subject=nid, weight=0.3, detail="Promiscuous mode is set to Accept."
                             + (" MAC address changes and forged transmits are also accepted." if sec["macChanges"] and sec["forgedTransmits"] else ""),
                             cause="Any VM on this port group can capture every frame on it. That is wanted for a firewall, an IDS "
                                   "or nested virtualisation, and a risk anywhere else.",
                             fix="If nothing on it needs it: Networking > Port groups > Edit > Security > Promiscuous mode: Reject.")
                elif sec["macChanges"] or sec["forgedTransmits"]:
                    what = " and ".join(x for x, on in (("MAC address changes", sec["macChanges"]), ("forged transmits", sec["forgedTransmits"])) if on)
                    self.add("network", f"spoof:{g['name']}", f"Port group {g['name']} accepts {what}", subject=nid, severity="info", weight=0,
                             detail="A VM on it may send traffic with an address other than its own.",
                             cause="Needed for some clustering and nested setups; otherwise it lets a VM impersonate another.",
                             fix="Set to Reject unless something on the port group needs it.")
                if g["vlan"] == 4095 and not g["kernel"]:
                    self.add("network", f"trunk:{g['name']}", f"Port group {g['name']} carries every VLAN (trunk)", subject=nid,
                             severity="info", weight=0, detail="VLAN ID 4095: tags are passed through to the VMs.",
                             cause="VMs on it can join any VLAN the physical switch sends to this host.",
                             fix="Fine for a router or firewall VM; other VMs belong on a port group with one VLAN.")

    # ------------------------------------------------------------------ the host's own doors
    def _security(self) -> None:
        for h in self.hosts:
            e = h["esx"]
            svc = {x["key"]: x for x in e["services"]}
            for key, what in (("TSM-SSH", "SSH"), ("TSM", "The ESXi Shell")):
                x = svc.get(key)
                if x and x["running"]:
                    stays = x["policy"] == "on"
                    self.add("security", f"{key.lower()}:{h['id']}", f"{what} is running on {h['name']}", subject=h["id"], weight=0.3,
                             detail=("It is set to start with the host, so it stays on." if stays else
                                     "It was started by hand and stops at the next reboot."),
                             cause="A root shell on the hypervisor reaches every VM's disks and memory. VMware's guidance is to "
                                   "keep it stopped and start it only while it is needed.",
                             fix="Host Client: Manage > Services > stop it" + (" and set its policy to Start and stop manually." if stays else "."))
            wide = [r for r in e["firewall"]["open"] if r["allIp"] and r["key"] in ("sshServer", "vSphereClient", "webAccess", "snmp")]
            if wide:
                self.add("security", f"mgmt-open:{h['id']}", f"{h['name']} accepts management connections from any address",
                         subject=h["id"], severity="info", weight=0,
                         detail="Open to all addresses: " + ", ".join(f"{r['label']} ({', '.join(r['ports']) or 'no port'})" for r in wide) + ".",
                         cause="The host's firewall can limit each service to the few addresses that administer it. Left open, "
                               "anything that can reach the management address can try to sign in.",
                         fix="Networking > Firewall rules > Edit settings: allow only your admin machines or subnet.")

    # ------------------------------------------------------------------ right-sizing
    def _sizing(self) -> None:
        note = self._watch_note()
        for n, target, freed in self._reclaimable():
            vm = n["vm"]
            self.add("sizing", f"mem:{n['id']}",
                     f"{n['name']} is allocated {_gb(vm['memMB'] * MB)} and has been actively using at most {_gb(vm['peakMemActiveMB'] * MB)}",
                     subject=n["id"], severity="info", weight=0,
                     detail=f"Active now: {_gb(vm['memActiveMB'] * MB)}. The host holds {_gb(vm['memHostMB'] * MB)} for it.",
                     cause="A guest touches all the memory it is given sooner or later (file cache), so the host has to keep it "
                           "even when the applications need a fraction.",
                     fix=f"{_gb(target * MB)} would be four times its busiest moment and free {_gb(freed * MB)}.",
                     options=[{"title": f"Reduce memory to {_gb(target * MB)}", "detail": "Shut the VM down, Edit settings > Memory, start it.",
                               "note": "check memory use inside the guest first"}], options_note=note)
        for n in self.on:
            vm = n["vm"]
            if vm["watchedS"] < MIN_WATCH_S or vm["peakCpuPct"] is None or vm["cpus"] < 4:
                continue
            fewer = suggest_vcpus(vm["cpus"], vm["peakCpuPct"])
            if fewer and vm["peakCpuPct"] < 0.25:
                self.add("sizing", f"cpu:{n['id']}",
                         f"{n['name']} has {vm['cpus']} vCPUs and has peaked at {_small_pct(vm['peakCpuPct'])} of them",
                         subject=n["id"], severity="info", weight=0, detail=f"Using {vm['cpuPct'] * 100:.0f}% now.",
                         cause="Unused vCPUs are not free: the host still has to find a core for each one, which makes this VM "
                               "and its neighbours wait longer.",
                         fix=f"{fewer} vCPU{'s' if fewer != 1 else ''} would keep its busiest moment under about 60%.",
                         options=[{"title": f"Reduce to {fewer} vCPU{'s' if fewer != 1 else ''}", "detail": "Shut the VM down, Edit settings > CPU, start it."}],
                         options_note=note)

    # ------------------------------------------------------------------ the hypervisor itself
    def _platform(self) -> None:
        import datetime
        for h in self.hosts:
            e = h["esx"]
            skew, ntp = e["clockSkewS"], e["ntp"]
            ntp_ok = ntp["running"] and bool(ntp["servers"])
            if skew is not None and abs(skew) >= 60:
                way = "fast" if skew > 0 else "slow"
                mins = abs(skew) / 60
                self.add("platform", f"clock:{h['id']}", f"{h['name']}'s clock is {mins:.0f} min {way}", subject=h["id"],
                         weight=0.45 if abs(skew) >= 300 else 0.35,
                         detail=f"{abs(skew):.0f} seconds {way}, compared with the clock of the machine this module runs on. "
                                + ("NTP is not running and no time servers are set." if not ntp_ok else f"NTP servers: {', '.join(ntp['servers'])}."),
                         cause="VMs take their time from the host when they start or resume, logs stop lining up with everything "
                               "else, and sign-ins that depend on the time (certificates, Kerberos) can fail.",
                         fix="Give the host time servers and start NTP.",
                         options=[{"title": "Set it in the Host Client", "detail": "Manage > System > Time & date > Edit NTP settings: "
                                   "Use Network Time Protocol, add servers (e.g. pool.ntp.org), policy Start and stop with host, then start the service."},
                                  {"title": "Or from the ESXi Shell (7.0 U1 and newer)", "command": "esxcli system ntp set --server=pool.ntp.org --enabled=true"}])
            elif not ntp_ok:
                self.add("platform", f"ntp:{h['id']}", f"{h['name']} is not keeping time with NTP", subject=h["id"], severity="info", weight=0,
                         detail="The NTP service is stopped or has no servers." + (f" The clock is within {abs(skew):.0f} s for now." if skew is not None else ""),
                         cause="Without it the host's clock drifts by seconds a day.", fix="Manage > System > Time & date > Edit NTP settings.")
            major = ".".join((e["version"] or "").split(".")[:2])
            eol = END_OF_SUPPORT.get(major)
            if eol and datetime.datetime.fromtimestamp(self.now, datetime.timezone.utc).date() > datetime.date(*eol):
                self.add("platform", f"eol:{h['id']}", f"ESXi {major} is past its end of general support", subject=h["id"], severity="info",
                         weight=0, detail=f"{(e['product'] or {}).get('fullName', 'ESXi ' + e['version'])}. General support for {major} ended on "
                                          f"{datetime.date(*eol).strftime('%d %B %Y').lstrip('0')}.",
                         cause="After that date the release no longer gets regular fixes, so newly found hypervisor flaws stay open.",
                         fix="Plan an upgrade if your licence and hardware allow it; until then keep the management address off untrusted networks.")
            if (e["hostName"] or "").startswith("localhost"):
                self.add("platform", f"hostname:{h['id']}", "The host has never been given a name", subject=h["id"], severity="info", weight=0,
                         detail=f"It still calls itself {e['hostName']}.", cause="Logs and certificates carry that name, which makes them hard to tell apart.",
                         fix="Networking > TCP/IP stacks > Default TCP/IP stack > Edit: set a host name.")
            if e["maintenance"]:
                self.add("platform", f"maintenance:{h['id']}", f"{h['name']} is in maintenance mode", subject=h["id"], weight=0.3,
                         detail="No VM can be powered on while it is.", cause="Someone put it there for maintenance.", fix="Exit maintenance mode when done.")
            for sn in e["sensors"]:
                state = (sn["state"] or "").lower()
                if state in ("red", "yellow"):
                    self.add("platform", f"sensor:{h['id']}:{sn['name']}", f"Hardware sensor: {sn['name']} is {'failing' if state == 'red' else 'degraded'}",
                             subject=h["id"], weight=0.7 if state == "red" else 0.35, detail=f"Reading: {sn['reading']:g} {sn['unit']}.",
                             cause="The host's own hardware monitoring raised it.", fix="Check the part (Monitor > Hardware in the Host Client).")

    def _monitoring(self) -> None:
        src = self.b.sources.get("vsphere") or {}
        if src.get("error"):
            self.add("platform", "source:vsphere", "The host cannot be read", severity="warning", weight=0.5,
                     detail=src["error"] + (f" Showing the last good reading, {round(src['ageS'] / 60)} min old." if src.get("ageS") else ""),
                     cause="Everything on this page is from before the fault until the host answers again.",
                     fix="Check that the host is up, reachable on its HTTPS port, and that the account still has its Read-only role.")
        for w in self.b.snap.get("warnings", [])[:3]:
            self.add("platform", f"source:{w[:40]}", "Part of the host could not be read", severity="info", weight=0, detail=w,
                     cause="What depends on it is missing from the page.", fix="Usually a permission the account lacks.")
