# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Read one ESXi host (or a vCenter) through the vSphere API and return plain dicts.

Read-only by construction: the only calls made are logins, property reads and performance queries,
so an account with the built-in Read-only role is enough. Nothing in this module can change the host.

`Vsphere.snapshot()` returns the same shape `sample.SampleHost.snapshot()` does, so the graph
builder and the checks never know which one they are looking at.
"""
from __future__ import annotations

import logging
import re
import ssl
import time
from datetime import datetime, timezone

log = logging.getLogger(__name__)

# 20-second realtime counters, averaged over the last few minutes. Units are vSphere's own.
VM_COUNTERS = {
    "cpu.usage.average": "cpuUsagePct",            # hundredths of a percent of the VM's own vCPUs
    "cpu.ready.summation": "cpuReadyMs",           # ms per 20 s the VM waited for a physical core
    "mem.active.average": "memActiveKb",
    "mem.swapinRate.average": "swapInKbps",
    "net.received.average": "netRxKbps",           # kilobytes per second
    "net.transmitted.average": "netTxKbps",
    "disk.read.average": "diskReadKbps",
    "disk.write.average": "diskWriteKbps",
    "disk.maxTotalLatency.latest": "diskLatencyMs",
}
HOST_COUNTERS = {
    "cpu.usage.average": "cpuUsagePct",
    "net.received.average": "netRxKbps",
    "net.transmitted.average": "netTxKbps",
    "disk.read.average": "diskReadKbps",
    "disk.write.average": "diskWriteKbps",
    "disk.maxTotalLatency.latest": "diskLatencyMs",
    "mem.swapinRate.average": "swapInKbps",
}
REALTIME = 20          # seconds per realtime sample
SAMPLES = 9            # three minutes
SNAPSHOT_FILE = re.compile(r"-\d{6}(-delta|-sesparse)?\.vmdk$|\.vmsn$|\.vmem$|-Snapshot\d+\.", re.I)


class VsphereError(Exception):
    pass


def _epoch(dt) -> float | None:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def guest_family(guest_id: str, full_name: str) -> str:
    text = f"{guest_id} {full_name}".lower()
    if "win" in text:
        return "windows"
    if any(k in text for k in ("linux", "ubuntu", "debian", "centos", "rhel", "red hat", "suse", "fedora", "alma", "rocky",
                               "oracle", "photon", "coreos", "amazon")):
        return "linux"
    if any(k in text for k in ("freebsd", "darwin", "mac os", "solaris")):
        return "unix"
    return "other"


class Vsphere:
    def __init__(self, host: str, user: str, password: str, *, port: int = 443, verify: bool = False, timeout: int = 20):
        self.host, self.user, self.password, self.port, self.verify, self.timeout = host, user, password, port, verify, timeout
        self._si = None
        self._counter_ids: dict[str, int] = {}
        self._counter_names: dict[int, str] = {}
        self.fetch_seconds = 0.0

    # ------------------------------------------------------------------ session
    def _connect(self):
        from pyVim.connect import SmartConnect
        from pyVmomi import vim
        ctx = None if self.verify else ssl._create_unverified_context()      # hosts ship a self-signed certificate
        try:
            self._si = SmartConnect(host=self.host, user=self.user, pwd=self.password, port=self.port, sslContext=ctx,
                                    connectionPoolTimeout=self.timeout, httpConnectionTimeout=self.timeout)
        except vim.fault.InvalidLogin as err:
            raise VsphereError("the host refused the user name or password") from err
        except vim.fault.NoPermission as err:
            raise VsphereError(f"the account {self.user} has no role on the host: give it the Read-only role "
                               "(Host > Permissions in the Host Client)") from err
        except (OSError, ssl.SSLError) as err:
            raise VsphereError(f"cannot reach https://{self.host}:{self.port}: {err}") from err
        self._counter_ids = {}
        return self._si

    def _content(self):
        from pyVmomi import vim, vmodl
        if self._si is not None:
            try:
                content = self._si.RetrieveContent()
                if content.sessionManager.currentSession is not None:
                    return content
            except (vim.fault.NotAuthenticated, vmodl.MethodFault, OSError, ssl.SSLError):
                pass
            self._si = None
        return self._connect().RetrieveContent()

    def close(self) -> None:
        if self._si is not None:
            try:
                from pyVim.connect import Disconnect
                Disconnect(self._si)
            except Exception:  # noqa: BLE001 - closing is best effort
                pass
            self._si = None

    # ------------------------------------------------------------------ bulk reads
    @staticmethod
    def _collect(content, obj_type, props: list[str]) -> list[tuple[object, dict]]:
        """Every object of one type with the named properties, in as few round trips as the server allows."""
        from pyVmomi import vim, vmodl
        pc = vmodl.query.PropertyCollector
        view = content.viewManager.CreateContainerView(content.rootFolder, [obj_type], True)
        try:
            spec = pc.FilterSpec(
                objectSet=[pc.ObjectSpec(obj=view, skip=True, selectSet=[
                    pc.TraversalSpec(name="view", type=vim.view.ContainerView, path="view", skip=False)])],
                propSet=[pc.PropertySpec(type=obj_type, pathSet=props)])
            out = []
            res = content.propertyCollector.RetrievePropertiesEx([spec], pc.RetrieveOptions())
            while res:
                for o in res.objects:
                    out.append((o.obj, {p.name: p.val for p in o.propSet}))
                if not getattr(res, "token", None):
                    break
                res = content.propertyCollector.ContinueRetrievePropertiesEx(res.token)
            return out
        finally:
            view.Destroy()

    def _perf(self, content, entities: list, wanted: dict[str, str]) -> dict[str, dict]:
        """-> {moid: {field: average over the last few minutes}}. Missing counters are simply absent."""
        from pyVmomi import vim
        pm = content.perfManager
        if not self._counter_ids:
            for c in pm.perfCounter:
                name = f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}"
                self._counter_ids[name] = c.key
                self._counter_names[c.key] = name
        ids = [(self._counter_ids[n], f) for n, f in wanted.items() if n in self._counter_ids]
        if not ids or not entities:
            return {}
        metric = [vim.PerformanceManager.MetricId(counterId=cid, instance="") for cid, _f in ids]
        field = dict(ids)
        out: dict[str, dict] = {}
        for i in range(0, len(entities), 50):
            specs = [vim.PerformanceManager.QuerySpec(entity=e, metricId=metric, intervalId=REALTIME, maxSample=SAMPLES)
                     for e in entities[i:i + 50]]
            for res in pm.QueryPerf(querySpec=specs) or []:
                row = out.setdefault(res.entity._moId, {})
                for series in res.value or []:
                    vals = [v for v in (series.value or []) if v is not None and v >= 0]
                    if vals and series.id.counterId in field:
                        f = field[series.id.counterId]
                        row[f] = max(vals) if f == "diskLatencyMs" else sum(vals) / len(vals)
        return out

    # ------------------------------------------------------------------ one reading
    def snapshot(self) -> dict:
        from pyVmomi import vim, vmodl
        started = time.monotonic()
        try:
            content = self._content()
            snap = self._read(content, vim)
        except VsphereError:
            raise
        except vim.fault.NoPermission as err:
            raise VsphereError(f"the account {self.user} may not read {getattr(err, 'object', 'the inventory')}: "
                               "give it the Read-only role on the host, with Propagate to children") from err
        except (vmodl.MethodFault, OSError, ssl.SSLError) as err:
            self._si = None
            raise VsphereError(f"reading {self.host} failed: {getattr(err, 'msg', None) or err}") from err
        self.fetch_seconds = round(time.monotonic() - started, 2)
        return snap

    def _read(self, content, vim) -> dict:
        now = time.time()
        about = content.about
        lic = None
        try:
            lic = next(iter(content.licenseManager.licenses or []), None)
        except Exception:  # noqa: BLE001 - not every role may read licences
            pass
        out = {
            "at": now,
            "about": {"fullName": about.fullName, "version": about.version, "build": about.build, "apiType": about.apiType,
                      "apiVersion": about.apiVersion, "licence": getattr(lic, "name", "") or about.licenseProductName or "",
                      "licenceEdition": getattr(lic, "editionKey", "") or "", "vcenter": about.apiType == "VirtualCenter"},
            "hosts": [], "datastores": [], "vms": [], "tasks": [], "warnings": [],
        }

        ds_name: dict[str, str] = {}
        for obj, p in self._collect(content, vim.Datastore, ["name", "summary", "vm"]):
            s = p.get("summary")
            if s is None:
                continue
            ds_name[obj._moId] = s.name
            out["datastores"].append({
                "id": obj._moId, "name": s.name, "type": s.type or "", "accessible": bool(s.accessible), "url": s.url or "",
                "capacity": int(s.capacity or 0), "free": int(s.freeSpace or 0), "uncommitted": int(s.uncommitted or 0),
                "maintenance": s.maintenanceMode or "normal", "vms": len(p.get("vm") or [])})

        host_objs = []
        for obj, p in self._collect(content, vim.HostSystem, [
                "name", "summary", "runtime", "hardware.cpuInfo", "hardware.systemInfo", "hardware.memorySize",
                "config.network", "config.firewall", "config.service", "config.dateTimeInfo", "config.lockdownMode"]):
            host_objs.append(obj)
            out["hosts"].append(self._host(obj, p, now))
        net_name = {}
        for obj, p in self._collect(content, vim.Network, ["name"]):
            net_name[obj._moId] = p.get("name", "")

        vm_objs = []
        for obj, p in self._collect(content, vim.VirtualMachine, [
                "name", "summary", "config", "guest", "runtime", "snapshot", "layoutEx", "datastore", "network", "resourcePool"]):
            try:
                out["vms"].append(self._vm(obj, p, ds_name, net_name, now))
                vm_objs.append(obj)
            except Exception as err:  # noqa: BLE001 - one odd VM must not hide the rest
                log.warning("skipping VM %s: %s", p.get("name", obj._moId), err)
                out["warnings"].append(f"could not read VM {p.get('name', obj._moId)}: {type(err).__name__}")

        try:
            on = [o for o, v in zip(vm_objs, out["vms"]) if v["powerState"] == "poweredOn"]
            perf = self._perf(content, on, VM_COUNTERS)
            for v in out["vms"]:
                v["perf"] = perf.get(v["id"], {})
            hperf = self._perf(content, host_objs, HOST_COUNTERS)
            for h in out["hosts"]:
                h["perf"] = hperf.get(h["id"], {})
        except Exception as err:  # noqa: BLE001 - the inventory is still worth showing without rates
            log.warning("performance counters unavailable: %s", err)
            out["warnings"].append(f"performance counters unavailable: {getattr(err, 'msg', None) or err}")
            for x in out["vms"] + out["hosts"]:
                x.setdefault("perf", {})

        try:
            for t in (content.taskManager.recentTask or [])[:30]:
                info = t.info
                out["tasks"].append({
                    "name": info.descriptionId or info.name or "", "entity": info.entityName or "", "state": str(info.state),
                    "user": (info.reason.userName if hasattr(info.reason, "userName") else "") or "",
                    "queued": _epoch(info.queueTime), "completed": _epoch(info.completeTime),
                    "error": (info.error.msg if info.error else "") or ""})
        except Exception as err:  # noqa: BLE001
            log.debug("recent tasks unavailable: %s", err)
        return out

    # ------------------------------------------------------------------ host
    @staticmethod
    def _host(obj, p: dict, now: float) -> dict:
        s, rt = p.get("summary"), p.get("runtime")
        hw, qs = s.hardware, s.quickStats
        cpu = p.get("hardware.cpuInfo")
        sysinfo = p.get("hardware.systemInfo")
        net = p.get("config.network")
        fw = p.get("config.firewall")
        svc = p.get("config.service")
        dti = p.get("config.dateTimeInfo")

        clock = None
        try:
            clock = obj.configManager.dateTimeSystem.QueryDateTime()
        except Exception as err:  # noqa: BLE001
            log.debug("host clock unreadable: %s", err)
        skew = round(_epoch(clock) - time.time(), 1) if clock else None

        def sec(policy) -> dict:
            sp = getattr(policy, "security", None)
            return {"promiscuous": bool(getattr(sp, "allowPromiscuous", False)), "macChanges": bool(getattr(sp, "macChanges", False)),
                    "forgedTransmits": bool(getattr(sp, "forgedTransmits", False))}

        pnic_by_key = {n.key: n for n in (net.pnic if net else [])}
        vswitches = [{
            "name": v.name, "ports": v.numPorts, "portsFree": v.numPortsAvailable, "mtu": v.mtu or 1500,
            "uplinks": [pnic_by_key[k].device for k in (v.pnic or []) if k in pnic_by_key],
            "security": sec(v.spec.policy) if v.spec else sec(None),
            "portgroups": [pg.split("-", 2)[-1] for pg in (v.portgroup or [])]} for v in (net.vswitch if net else [])]
        vmk_groups = {v.portgroup for v in (net.vnic if net else []) if v.portgroup}
        portgroups = [{
            "name": g.spec.name, "vswitch": g.spec.vswitchName, "vlan": g.spec.vlanId or 0, "security": sec(g.computedPolicy),
            "kernel": g.spec.name in vmk_groups} for g in (net.portgroup if net else [])]
        pnics = [{
            "device": n.device, "mac": n.mac or "", "driver": n.driver or "", "linkUp": n.linkSpeed is not None,
            "speedMb": n.linkSpeed.speedMb if n.linkSpeed else 0, "duplex": bool(n.linkSpeed.duplex) if n.linkSpeed else False}
            for n in (net.pnic if net else [])]
        vmknics = [{"device": v.device, "portgroup": v.portgroup or "", "ip": v.spec.ip.ipAddress if v.spec and v.spec.ip else "",
                    "mac": v.spec.mac if v.spec else ""} for v in (net.vnic if net else [])]

        rulesets = []
        for r in (fw.ruleset if fw else []):
            ah = r.allowedHosts
            rulesets.append({
                "key": r.key, "label": r.label or r.key, "enabled": bool(r.enabled), "required": bool(r.required),
                "allIp": bool(ah.allIp) if ah else True,
                "allowed": ([a for a in (ah.ipAddress or [])] + [f"{n.network}/{n.prefixLength}" for n in (ah.ipNetwork or [])]) if ah else [],
                "rules": [{"port": x.port, "endPort": x.endPort or None, "direction": str(x.direction), "protocol": x.protocol}
                          for x in (r.rule or [])][:12]})
        services = [{"key": x.key, "label": x.label or x.key, "running": bool(x.running), "policy": x.policy or ""}
                    for x in (svc.service if svc else [])]
        sensors = []
        hs = getattr(rt, "healthSystemRuntime", None)
        for sn in (getattr(getattr(hs, "systemHealthInfo", None), "numericSensorInfo", None) or []):
            sensors.append({"name": sn.name, "type": sn.sensorType or "", "state": getattr(sn.healthState, "key", "") or "",
                            "reading": (sn.currentReading or 0) * (10 ** (sn.unitModifier or 0)), "unit": sn.baseUnits or ""})
        return {
            "id": obj._moId, "name": p.get("name") or s.config.name,
            "vendor": hw.vendor or "", "model": hw.model or "", "cpuModel": hw.cpuModel or "",
            "serial": next((i.identifierValue for i in (getattr(sysinfo, "otherIdentifyingInfo", None) or [])
                            if getattr(i.identifierType, "key", "") == "ServiceTag"), ""),
            "sockets": hw.numCpuPkgs or 0, "cores": hw.numCpuCores or 0, "threads": hw.numCpuThreads or 0,
            "cpuMhz": hw.cpuMhz or 0, "hz": getattr(cpu, "hz", 0) or 0,
            "memBytes": int(p.get("hardware.memorySize") or hw.memorySize or 0),
            "cpuUsedMhz": qs.overallCpuUsage or 0, "memUsedBytes": (qs.overallMemoryUsage or 0) * 2**20,
            "uptimeS": qs.uptime or 0, "bootTime": _epoch(rt.bootTime),
            "connectionState": str(rt.connectionState), "powerState": str(rt.powerState),
            "maintenance": bool(rt.inMaintenanceMode), "overallStatus": str(s.overallStatus),
            "product": {"name": s.config.product.name, "version": s.config.product.version, "build": s.config.product.build,
                        "fullName": s.config.product.fullName} if s.config and s.config.product else {},
            "managementIp": getattr(s, "managementServerIp", None) or "",
            "lockdown": str(p.get("config.lockdownMode") or "lockdownDisabled"),
            "clockSkewS": skew,
            "ntp": {"servers": list(getattr(getattr(dti, "ntpConfig", None), "server", None) or []),
                    "running": next((x["running"] for x in services if x["key"] == "ntpd"), False),
                    "policy": next((x["policy"] for x in services if x["key"] == "ntpd"), "")},
            "services": services, "firewall": {"rulesets": rulesets}, "sensors": sensors,
            "vswitches": vswitches, "portgroups": portgroups, "pnics": pnics, "vmknics": vmknics,
        }

    # ------------------------------------------------------------------ VM
    @staticmethod
    def _vm(obj, p: dict, ds_name: dict, net_name: dict, now: float) -> dict:
        from pyVmomi import vim
        s, cfg, guest, rt = p.get("summary"), p.get("config"), p.get("guest"), p.get("runtime")
        qs = s.quickStats
        sc = s.config
        devices = list(cfg.hardware.device) if cfg and cfg.hardware else []
        nics, disks, cdroms = [], [], []
        for d in devices:
            label = d.deviceInfo.label if d.deviceInfo else ""
            if isinstance(d, vim.vm.device.VirtualEthernetCard):
                b = d.backing
                network = getattr(b, "deviceName", "") or ""
                if not network and getattr(b, "port", None) is not None:
                    network = f"dvportgroup {b.port.portgroupKey}"
                nics.append({"label": label, "mac": d.macAddress or "", "network": network,
                             "type": type(d).__name__.split(".")[-1].replace("Virtual", ""),
                             "connected": bool(d.connectable.connected) if d.connectable else False,
                             "startConnected": bool(d.connectable.startConnected) if d.connectable else False})
            elif isinstance(d, vim.vm.device.VirtualDisk):
                b = d.backing
                file_name = getattr(b, "fileName", "") or ""
                m = re.match(r"\[(.+?)\]", file_name)
                disks.append({"label": label, "capacity": int(d.capacityInBytes or (d.capacityInKB or 0) * 1024),
                              "datastore": m.group(1) if m else "", "file": file_name,
                              "thin": bool(getattr(b, "thinProvisioned", False)), "mode": getattr(b, "diskMode", "") or "",
                              "delta": bool(getattr(b, "parent", None))})
            elif isinstance(d, vim.vm.device.VirtualCdrom):
                b = d.backing
                iso = getattr(b, "fileName", "") if isinstance(b, vim.vm.device.VirtualCdrom.IsoBackingInfo) else ""
                cdroms.append({"label": label, "connected": bool(d.connectable.connected) if d.connectable else False,
                               "startConnected": bool(d.connectable.startConnected) if d.connectable else False, "iso": iso or ""})

        snaps: list[dict] = []

        def walk(tree, depth=0):
            for t in tree or []:
                snaps.append({"name": t.name, "description": t.description or "", "created": _epoch(t.createTime), "depth": depth,
                              "memory": str(t.state) == "poweredOn", "quiesced": bool(t.quiesced)})
                walk(t.childSnapshotList, depth + 1)
        if p.get("snapshot") is not None:
            walk(p["snapshot"].rootSnapshotList)
        snap_bytes = 0
        lx = p.get("layoutEx")
        if lx is not None and snaps:
            snap_bytes = sum(int(f.size or 0) for f in (lx.file or []) if SNAPSHOT_FILE.search(f.name or ""))

        ips = []
        for n in (guest.net if guest else []) or []:
            ips += [ip for ip in (n.ipAddress or []) if ":" not in ip]
        if guest and guest.ipAddress and guest.ipAddress not in ips and ":" not in guest.ipAddress:
            ips.insert(0, guest.ipAddress)
        guest_disks = [{"path": g.diskPath, "capacity": int(g.capacity or 0), "free": int(g.freeSpace or 0)}
                       for g in ((guest.disk if guest else None) or [])]
        full = (guest.guestFullName if guest else "") or sc.guestFullName or ""
        gid = (guest.guestId if guest else "") or sc.guestId or ""
        st = s.storage
        return {
            "id": obj._moId, "name": p.get("name") or sc.name, "uuid": sc.uuid or "", "template": bool(sc.template),
            "annotation": (sc.annotation or "").strip(),
            "powerState": str(rt.powerState), "connectionState": str(rt.connectionState), "overallStatus": str(s.overallStatus),
            "guestId": gid, "guestFullName": full, "family": guest_family(gid, full),
            "hostname": (guest.hostName if guest else "") or "", "ips": ips[:8],
            "tools": {"status": str(guest.toolsVersionStatus2 or guest.toolsVersionStatus or "") if guest else "",
                      "running": str(guest.toolsRunningStatus or "") if guest else "", "version": (guest.toolsVersion if guest else "") or ""},
            "guestDisks": guest_disks,
            "numCpu": sc.numCpu or 0, "coresPerSocket": (cfg.hardware.numCoresPerSocket if cfg and cfg.hardware else 0) or 0,
            "memMB": sc.memorySizeMB or 0, "cpuReservation": sc.cpuReservation or 0, "memReservationMB": sc.memoryReservation or 0,
            "cpuLimit": (cfg.cpuAllocation.limit if cfg and cfg.cpuAllocation else -1),
            "memLimitMB": (cfg.memoryAllocation.limit if cfg and cfg.memoryAllocation else -1),
            "hwVersion": (cfg.version if cfg else "") or "", "firmware": (cfg.firmware if cfg else "") or "",
            "changed": _epoch(cfg.modified) if cfg and cfg.modified and cfg.modified.year > 1971 else None,
            "bootTime": _epoch(rt.bootTime), "uptimeS": qs.uptimeSeconds or 0,
            "consolidationNeeded": bool(getattr(rt, "consolidationNeeded", False)),
            "question": (rt.question.text if getattr(rt, "question", None) else "") or "",
            "nics": nics, "disks": disks, "cdroms": cdroms,
            "datastores": sorted({ds_name.get(d._moId, "") for d in (p.get("datastore") or [])} - {""}),
            "networks": sorted({net_name.get(n._moId, "") for n in (p.get("network") or [])} - {""}),
            "resourcePool": getattr(p.get("resourcePool"), "_moId", "") or "",
            "hostId": getattr(getattr(rt, "host", None), "_moId", "") or "",
            "committed": int(st.committed or 0) if st else 0, "uncommitted": int(st.uncommitted or 0) if st else 0,
            "provisioned": sum(d["capacity"] for d in disks),
            "quick": {"cpuMhz": qs.overallCpuUsage or 0, "cpuDemandMhz": qs.overallCpuDemand or 0,
                      "hostMemMB": qs.hostMemoryUsage or 0, "guestMemMB": qs.guestMemoryUsage or 0,
                      "balloonMB": qs.balloonedMemory or 0, "swappedMB": qs.swappedMemory or 0,
                      "compressedMB": (qs.compressedMemory or 0) / 1024, "sharedMB": qs.sharedMemory or 0,
                      "overheadMB": qs.consumedOverheadMemory or 0},
            "maxCpuMhz": rt.maxCpuUsage or 0,
            "snapshots": snaps, "snapshotBytes": snap_bytes,
        }


def utc(ts: float | None) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else ""
