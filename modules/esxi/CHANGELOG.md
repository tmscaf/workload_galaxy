# Changelog

## 0.1.0

First version. Built on the shared `galaxy_core` package.

- **Reads one ESXi host** (or a vCenter's inventory) through the vSphere API with a read-only
  account. Nothing on the host is changed and nothing is installed on it.
- **Map.** Galaxy: the host at the centre, VMs in one arm per group and sized by memory,
  datastores and port groups on the rim, physical network cards beyond them. Strata: networks,
  VMs, each VM's running slot and disks, and the host and datastores underneath. Powered-off
  VMs stay on the map, dimmed, with their disks.
- **Triage.** Findings in eight categories (capacity, performance, storage and snapshots, VM
  setup, networking, host security, right-sizing, platform), each with why it matters and what
  to do, and sized "Ways to fix it" for host memory, datastore space, snapshots and CPU wait.
- **Tables.** Every VM (state, vCPU, memory, CPU, CPU wait, active memory, disk held,
  snapshots) and every attachment, filtered, sorted and paged by the collector.
- **Right-sizing** from the busiest each VM has been over up to 14 days, starting after an hour
  of watching.
- **What changed**, worked out by comparing readings: power, snapshots, resizes, VMs added or
  removed. (A host on the free licence does not serve its event history.)
- **Sample mode** (`ESXIGALAXY_MODE=sample`): a made-up host with one of each kind of finding.
- **Settings declared once** (`config.FIELDS`), served as a manifest at `/api/v1/module` for the
  suite's module page.
