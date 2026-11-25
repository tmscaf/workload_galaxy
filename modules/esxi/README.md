# ESXi Galaxy

A live map of a VMware ESXi host: every virtual machine, the networks and datastores it hangs
off, how full the host is, and what needs attention. A module of the
[Workload Galaxy](../../README.md) suite.

It reads the host through the vSphere API with a read-only account. It changes nothing on the
host and installs nothing on it.

| On the map | Means |
|---|---|
| The box at the centre | The ESXi host |
| A star | A virtual machine, grouped into arms, sized by the memory it has been given |
| A dimmed star | A VM that is powered off (it still holds disk space) |
| An orange point on the rim | A datastore; lines to it carry each VM's disk reads and writes |
| A green point on the rim | A port group (a virtual network); lines to it carry each VM's network traffic |
| A gold point beyond the rim | A physical network card carrying a virtual switch out of the host |
| A dashed line | Attached but not live: a powered-off VM's disks, an unplugged adapter |

**Strata** shows the same thing as stacked planes: networks, virtual machines, what each VM uses
(its running slot on the host and each of its disks), and the host and datastores underneath.

**Triage** is the page to start on: how full the host is, what is busiest, and every finding with
why it matters and what to do.

## What it reports

| Category | Examples |
|---|---|
| Capacity | Host memory nearly full; more memory promised to VMs than the host has; many vCPUs per core; a VM having memory ballooned or swapped by the host |
| Performance | A VM waiting for a physical core (CPU ready time); disk latency; the host's CPU nearly full |
| Storage & snapshots | A datastore filling up or inaccessible; thin disks promising more than the datastore holds; old or large snapshots; disks needing consolidation; a nearly full filesystem inside a guest; powered-off VMs holding space |
| VM setup | VMware Tools missing, stopped or out of date; a network adapter not connected; an ISO left attached; a VM stopped waiting for an answer |
| Networking | A virtual switch with a single uplink; an uplink with no link, or linked below 1 Gbps or at half duplex; a port group in promiscuous mode, accepting forged transmits or MAC changes, or trunking every VLAN |
| Host security | SSH or the ESXi Shell running; management services open to any address in the host's firewall |
| Right-sizing | VMs given far more memory or vCPUs than they have used, with a suggested size (information only) |
| Platform | The host's clock out of step and NTP off; a release past its end of general support; hardware sensors reporting trouble; maintenance mode; the host not answering |

Several findings come with **Ways to fix it**: concrete options with numbers, such as which VMs
could give memory back and how much that frees, or which snapshots and powered-off VMs hold the
most space on a full datastore.

### How sizing advice is worked out

The module remembers the busiest each VM has been, per day, for 14 days. A memory suggestion is
four times the most memory the VM has had in active use, never under 2 GB, and is only made when
it at least halves the allocation. A vCPU suggestion keeps the busiest moment under about 60%.
Nothing is suggested until a VM has been watched for an hour.

"Active" memory is the host's own estimate, made by sampling which pages a guest touched
recently. It leaves out file cache and reads low for anything that sits on a large cache (a
database, a JVM), so treat a suggestion as a prompt to check inside the guest, not as a number
to apply.

## Where the data comes from

One HTTPS connection to the host's vSphere API (port 443), kept open and reused. Each refresh
(every 30 seconds by default) reads:

- the host: hardware, usage, virtual switches, port groups, physical cards, firewall rule sets,
  services, time settings, hardware sensors
- every VM: configuration, power state, guest details from VMware Tools, disks, adapters,
  snapshots, storage used
- every datastore: capacity, free space, what has been promised
- 20-second performance counters for the host and each running VM, averaged over three minutes

The account needs the built-in **Read-only** role and nothing else. The free ESXi licence is
enough: it limits changes through the API, and this module makes none.

## Set up the account

In the ESXi Host Client:

1. **Manage > Security & users > Users > Add user.** Pick a name and a password.
2. Right-click **Host** in the navigator, **Permissions > Manage permissions > Add user**. Choose
   the user, the role **Read-only**, and leave **Propagate to all children** ticked.

Creating the user is not enough on its own: without step 2 the host accepts the password and
then refuses every read.

## Run it without a host

```bash
cd modules/esxi
python3 -m venv .venv && .venv/bin/pip install -r collector/requirements.txt
cd collector
PYTHONPATH=../../../core/py ESXIGALAXY_MODE=sample ../.venv/bin/uvicorn esxigalaxy.main:app --port 8097
# open http://127.0.0.1:8097/
```

`sample` mode serves a made-up host (15 VMs, two datastores, two virtual switches) with one of
each kind of finding.

## Run it against a host

Same as above, with the host in place of the sample:

```bash
PYTHONPATH=../../../core/py ESXIGALAXY_HOST=192.168.1.10 ESXIGALAXY_USER=galaxy ESXIGALAXY_PASSWORD='...' \
  ../.venv/bin/uvicorn esxigalaxy.main:app --port 8097
```

Or as a container. The image is built from the repository root, because it includes the shared
core package:

```bash
docker build -f modules/esxi/Dockerfile -t esxi-galaxy .
docker run --rm -p 8097:8080 --read-only --tmpfs /tmp \
  -e ESXIGALAXY_HOST=192.168.1.10 -e ESXIGALAXY_USER=galaxy -e ESXIGALAXY_PASSWORD='...' esxi-galaxy
```

## Deploy to Kubernetes

`deploy/k8s` is the base: one Deployment, a Service and a small volume for the history. It runs
as non-root with a read-only root filesystem, no capabilities and no access to the Kubernetes
API. Two things must exist in the namespace:

- ConfigMap `esxi-galaxy-config` with `ESXIGALAXY_HOST` and any other settings
- Secret `esxi-galaxy-host` with keys `user` and `password`

`deploy/homelab` is an overlay that supplies both for one home lab, and
`scripts/deploy-homelab.sh` builds, ships and applies it:

```bash
ESXI_USER=<user> ESXI_PASSWORD=<password> scripts/deploy-homelab.sh   # first time
scripts/deploy-homelab.sh                                             # after that
```

## The site file

By default VMs are grouped by guest OS (Linux, Windows, BSD & Unix, Other). A site file sorts
them into groups of your own, by name:

```json
{
  "groups": [
    {"id": "web", "title": "Web servers", "match": ["web-*", "proxy-*"]},
    {"id": "data", "title": "Databases", "match": ["db-*", "*-cache"]}
  ]
}
```

Patterns are shell-style (`*`, `?`), compared without regard to case; the first group that
matches wins, and a VM that matches none falls back to its guest OS. Templates always get their
own group. Point `ESXIGALAXY_SITE_FILE` at the file.

## Configuration

Every setting is an environment variable. The same list, with labels and help text, is served
at `/api/v1/module` for the suite's module page.

| Variable | Default | What it is |
|---|---|---|
| `ESXIGALAXY_MODE` | live | `live` reads a real host; `sample` shows a made-up host with one of each kind of finding |
| `ESXIGALAXY_HOST` | | Name or IP address of the ESXi host (or vCenter) |
| `ESXIGALAXY_USER` | | An account with the built-in Read-only role on the host |
| `ESXIGALAXY_PASSWORD` | | Its password |
| `ESXIGALAXY_PORT` | 443 | HTTPS port of the vSphere API |
| `ESXIGALAXY_VERIFY_TLS` | off | Check the host's certificate. Off by default because hosts ship a self-signed one |
| `ESXIGALAXY_NAME` | | Display name in the top bar. Empty uses the host's own name |
| `ESXIGALAXY_REFRESH_SECONDS` | 30 | How often to read the host. Its counters are 20-second samples, so under 20 gains nothing |
| `ESXIGALAXY_SITE_FILE` | | JSON file that sorts VMs into named groups |
| `ESXIGALAXY_DATA_DIR` | | Where usage peaks and the change list are kept between restarts. Empty keeps them in memory |
| `ESXIGALAXY_DATASTORE_WARN_PCT` | 80 | Datastore warning at this percentage used |
| `ESXIGALAXY_DATASTORE_CRIT_PCT` | 90 | Datastore critical at this percentage used |
| `ESXIGALAXY_MEMORY_WARN_PCT` | 90 | Host memory warning at this percentage used |
| `ESXIGALAXY_SNAPSHOT_DAYS` | 7 | A snapshot counts as old after this many days |
| `ESXIGALAXY_READY_WARN_PCT` | 5 | Report a VM when its vCPUs wait this long for a physical core (percent per vCPU) |
| `ESXIGALAXY_WEB_ROOT` | | Folder the UI is served from (set by the image) |
| `ESXIGALAXY_LOG_LEVEL` | INFO | Log level |

## Limits

- **Tested against one host:** a standalone ESXi 7.0 U3 on the free licence. A vCenter answers
  the same API calls, but that path has not been run, and clusters, vMotion and DRS are not
  drawn.
- **Distributed switches are not drawn.** An adapter on a distributed port group is listed on
  its VM but has no line on the map.
- **No view of who talks to whom.** The host knows how much each VM sends and receives, not
  with whom. Lines to port groups show volume only.
- **Changes are noticed, not read from the host.** The list starts when the module does (plus
  snapshots and boots the host itself dates), and anything that happens while it is down is
  seen only as a difference afterwards.
- **Snapshot sizes are estimates**, added up from the snapshot files' names.
- **Guest details need VMware Tools:** address, host name and filesystem usage are missing for
  a VM without it.
- **The clock check compares the host with the machine this module runs on.** If that machine's
  clock is wrong, the finding is too.
- **Hardware health needs the host to expose sensors.** Many self-built machines expose none.
- **There is no sign-in.** Anyone who can reach the page can read it. Keep it on a trusted
  network or behind a proxy that authenticates.

## Layout

```
collector/esxigalaxy/   vsphere.py (the reader), sample.py (sample host), graph.py (builder),
                        findings.py (checks and sizing), history.py (peaks and changes),
                        state.py (refresh loop), config.py (settings and manifest), main.py (API)
web/                    the front end (Three.js, no build step)
deploy/k8s, deploy/homelab, scripts/deploy-homelab.sh
tests/
```

The API, the refresh loop, the tables and the graph format come from
[`core/py/galaxy_core`](../../core/py/galaxy_core).

Tests: `.venv/bin/pip install pytest httpx` once, then `.venv/bin/python -m pytest -q tests`
from this directory.
