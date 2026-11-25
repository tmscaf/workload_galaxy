# Changelog

## 0.2.0

- **Settings from the suite.** The host, account, display name, VM groups and thresholds can be
  set from the suite's Settings page instead of at deploy time. Saved settings are kept in the
  data folder (`settings.json`, readable only by the module), win over the environment, and are
  applied at once: the module starts a fresh reading without a restart.
- **Test connection** tries the host with what is typed, without saving it.
- **The password is write-only** and is forgotten when the host, user or port changes, so it is
  never sent to a different host by accident.
- **Changes need the settings token** (`ESXIGALAXY_SETTINGS_TOKEN`), which the suite shell
  presents. Without one the module takes its settings from its environment only, as before,
  and does not reveal them.
- **VM groups as a setting** (`ESXIGALAXY_GROUPS`): one `Title: pattern, pattern` per line. A
  site file still works; the setting wins.
- **Installs with nothing configured.** The Kubernetes base no longer requires a ConfigMap or a
  Secret; an unconfigured module says what it is missing and waits for its settings.
- In the suite's frame the module no longer repeats the logo.

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
