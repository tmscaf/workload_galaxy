# Workload Galaxy

[![Tests](https://github.com/tmscaf/workload_galaxy/actions/workflows/tests.yml/badge.svg)](https://github.com/tmscaf/workload_galaxy/actions/workflows/tests.yml)
[![Licence: AGPL v3](https://img.shields.io/badge/licence-AGPL--3.0-blue.svg)](LICENSE)

A suite of live, explorable maps of the systems you run. Each module watches one kind of
system, draws it as an animated 3D galaxy, and tells you in plain words what is wrong,
why, and what to do about it.

![The Galaxy view of a Kubernetes cluster on a bad release day](docs/screenshots/kubernetes-galaxy.png)

Every screenshot on this page comes from the built-in demo data, so you can reproduce all
of them on a laptop with no cluster and no router ([Try it](#try-it)).

## Modules

| Module | Watches | Version | Docs |
|---|---|---|---|
| [Kubernetes](modules/kubernetes) | Kubernetes and OpenShift projects: workloads, pods, hosts, who calls whom, and where latency starts | 0.12.0 | [README](modules/kubernetes/README.md) · [changelog](modules/kubernetes/CHANGELOG.md) |
| [Network](modules/network) | A home or small-office network behind a pfSense router: every device, what it talks to, what the firewall turns away, Wi-Fi and VPN | 0.3.0 | [README](modules/network/README.md) · [changelog](modules/network/CHANGELOG.md) |
| [ESXi](modules/esxi) | A VMware ESXi host: virtual machines, the networks and datastores they hang off, capacity and right-sizing | 0.2.0 | [README](modules/esxi/README.md) · [changelog](modules/esxi/CHANGELOG.md) |

Each module is a separate service with its own image, version and deployment. They share
a design, a graph format and an API shape, described under [How it fits together](#how-it-fits-together).

## One bar for all of them

The [shell](shell) puts the modules behind one address. Its bar carries the suite's name and a
drop-down that switches module; each entry shows what that module is watching and how many
things there need attention, so trouble in one is visible while you are looking at another.

![The suite bar with the module selector open](docs/screenshots/suite-bar.png)

**Settings**, at the right of the bar, is where each module is told what to read: its host, its
account, how things are grouped. It is behind one admin sign-in; a module's form is drawn from
the settings the module declares, values can be tested before they are saved, and passwords are
never shown back. Saved settings take effect at once, with no redeploy.

Each module also has a switch there. Turn off the ones you do not use and they leave the
drop-down; turn one on and it is back.

![The settings area with a module's form](docs/screenshots/suite-settings.png)

All three modules are configured this way. ESXi applies a change at once; Kubernetes and
Network restart themselves with it, which takes a few seconds. Anything set where a module is
deployed still works, and what is saved from Settings wins over it.

## Kubernetes module

Start in **Triage** when something is broken. It lists every problem with its likely
cause, the evidence, copy-ready `oc` / `kubectl` commands, and sized fixes ("raise the CPU
limit from 500m to 1"). Symptoms are folded under the root cause they clear with.

![Triage: 13 problems, grouped by project, with ways to fix each](docs/screenshots/kubernetes-triage.png)

Click anything on the map for its diagnosis, latest rollout, warning events and status.

![A deployment selected in the Galaxy view, with its diagnosis and fixes](docs/screenshots/kubernetes-galaxy-detail.png)

The same cluster in three more views, all sharing one selection:

| View | Shows |
|---|---|
| **Strata** | Stacked planes: end users, services, pods and containers, hosts |
| **Cascade** | The call graph as a left-to-right waterfall, one column per hop, with failures flowing back from their root cause |
| **Gibson** | The cluster as a city: towers are workloads, floors are pods, traces are calls |

![Strata view with a StatefulSet selected](docs/screenshots/kubernetes-strata.png)

![Cascade view](docs/screenshots/kubernetes-cascade.png)

![Gibson view](docs/screenshots/kubernetes-gibson.png)

It reads the cluster API, and optionally mesh metrics and a node agent (a DaemonSet that
reports live flows, kernel round-trip times, retransmits and queue backpressure). It scales
to large estates by rolling healthy projects up and drawing only what is failing. See the
[module README](modules/kubernetes/README.md) for what it can diagnose, deployment on
k3s / Rancher and OpenShift, configuration and security.

## Network module

A star is a device, a point beyond the gateway is an internet service (grouped by name,
not address), and a line is traffic.

![The Galaxy view of a home network](docs/screenshots/network-galaxy.png)

**Triage** reports findings in plain words: a device talking to a known-bad address,
unusual upload volume, port forwards open to everyone, port scans, repeated blocked
lookups, newly joined devices.

![Network triage](docs/screenshots/network-triage.png)

**Wi-Fi** shows where a connection breaks: latency and loss hop by hop, each access
point's channel load, and every wireless device's signal and drops. A **VPN** tab lists
who is connected and failed logins.

![Wi-Fi: the shared path, access points and per-device signal](docs/screenshots/network-wifi.png)

It reads ntopng and the firewall's syslog on a pfSense router, and optionally a UniFi
controller. Addresses can be blocked from the page behind a separate action password;
viewing needs none. See the [module README](modules/network/README.md) for router setup,
the site file and configuration.

## ESXi module

The host sits at the centre, its VMs orbit it by group and are sized by the memory they have
been given, and what they hang off sits on the rim: datastores, port groups, and the physical
network cards behind them. Powered-off VMs stay on the map, dimmed, because they still hold
disk space.

![The Galaxy view of an ESXi host](docs/screenshots/esxi-galaxy.png)

**Triage** shows how full the host is and every finding with sized ways to fix it: which VMs
could give memory back and how much that frees, which snapshots and powered-off VMs hold the
most space on a full datastore.

![ESXi triage: capacity, groups and findings](docs/screenshots/esxi-triage.png)

Click a VM for what it is using now, the busiest it has been, its disks, adapters and
snapshots. **Strata** stacks the same host as networks, VMs, each VM's running slot and disks,
and the host and datastores underneath.

![A VM selected, with its snapshot finding and usage](docs/screenshots/esxi-galaxy-detail.png)

![Strata view of an ESXi host](docs/screenshots/esxi-strata.png)

It reads the host through the vSphere API with a read-only account, changes nothing and
installs nothing on it; the free ESXi licence is enough. See the
[module README](modules/esxi/README.md) for the account setup, what it checks, and its limits.

## Try it

Every module runs on made-up data with nothing to connect to.

**Kubernetes**, in Docker (a simulated OpenShift cluster with six deployments failing six ways):

```bash
modules/kubernetes/scripts/demo-local.sh        # opens http://127.0.0.1:8099/?view=triage
modules/kubernetes/scripts/demo-local.sh stop
```

The **Simulate** menu in the top bar switches between five presets and 24 single fault
scenarios. `demo-local.sh enterprise` simulates a 600-project estate instead.

**Network**, from a Python virtual environment (a sample network of 16 devices with one of
each kind of finding):

```bash
cd modules/network
python3 -m venv .venv && .venv/bin/pip install -r collector/requirements.txt
cd collector
NETGALAXY_MODE=sample NETGALAXY_SYSLOG_PORT=0 NETGALAXY_SINKHOLE_IPS=10.99.0.1 \
  NETGALAXY_ROUTER_IP=10.20.0.1 NETGALAXY_LAN_CIDRS=10.20.0.0/24 PYTHONPATH=../../../core/py \
  ../.venv/bin/uvicorn netgalaxy.main:app --port 8098
# open http://127.0.0.1:8098/
```

**ESXi**, the same way (a sample host with 15 VMs and one of each kind of finding):

```bash
cd modules/esxi
python3 -m venv .venv && .venv/bin/pip install -r collector/requirements.txt
cd collector
PYTHONPATH=../../../core/py ESXIGALAXY_MODE=sample ../.venv/bin/uvicorn esxigalaxy.main:app --port 8097
# open http://127.0.0.1:8097/
```

**The shell**, once any of the modules above are running (use the ports you started them on):

```bash
cd shell
python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt
cd app
PYTHONPATH=../../core/py GALAXY_MODULES="kubernetes=http://127.0.0.1:8099,network=http://127.0.0.1:8098,esxi=http://127.0.0.1:8097" \
  ../.venv/bin/uvicorn galaxyshell.main:app --port 8089
# open http://127.0.0.1:8089/
```

## How it fits together

Every module has the same three parts:

| Part | What it is |
|---|---|
| `collector/` | A Python (FastAPI) service that reads its data source, builds a graph of nodes and calls, scores risk, and serves the API and the UI from one container |
| `web/` | The UI: Three.js, vendored, no build step and no CDN, so it works air-gapped |
| `deploy/` | Kustomize manifests; the collector runs as a non-root container |

They also share an API shape under `/api/v1/` (`meta`, `topology`, `node`, `workloads`,
`calls`, `search`, `hotspots`, `triage`) and an embedding contract: any module's UI runs
in an iframe with `?embed=1` and exchanges selection and view changes over `postMessage`.

The shared part lives in `core/py/galaxy_core`: declaring a module's settings once, building
nodes, edges and findings in the graph format, the refresh loop, and the API above. The ESXi
module is built on it. The Kubernetes and Network modules came first: they use the core for
their settings, and still carry their own copies of the rest.

Every module serves a manifest at `/api/v1/module` (what it is, what it
needs, and the settings it takes) and a settings API the suite's Settings page reads and writes.
A module accepts changes only from a caller that presents the settings token it shares with the
shell.

```
core/
  py/galaxy_core/   settings, graph builders, scoping, tables, refresh loop, shared API; tests
modules/
  kubernetes/   collector, node agent, web UI, deploy (k8s base, openshift overlay), tests
  network/      collector, web UI, deploy (k8s base, router sync script), tests
  esxi/         collector, web UI, deploy (k8s base), tests
shell/            the suite bar and module selector: app, web UI, deploy (k8s base), tests
tools/            deploy-k3s.sh (build, ship and apply one part) and cluster-secrets.sh (one private
                  secrets file on the cluster -> Kubernetes Secrets)
docs/
  screenshots/
```

Tests run per module, from a virtual environment in the module directory:

```bash
cd modules/kubernetes        # or modules/network
python3 -m venv .venv && .venv/bin/pip install -r collector/requirements.txt pytest
.venv/bin/python -m pytest -q tests
```

## Secrets

No password or token belongs in this repository, in a deploy command, or on a laptop. On a
cluster, every secret the suite needs lives in one private file on the cluster machine
(`~/.config/workload-galaxy/secrets.env`, readable only by its owner), and
[`tools/cluster-secrets.sh`](tools/cluster-secrets.sh) turns that file into the Kubernetes
Secrets each module reads:

```bash
ssh <cluster> 'bash -s -- init' < tools/cluster-secrets.sh   # once: build the file
ssh <cluster> 'bash -s -- list' < tools/cluster-secrets.sh   # which values are set (never the values)
ssh <cluster> 'bash -s -- sync' < tools/cluster-secrets.sh   # file -> Kubernetes Secrets
```

`init` copies in whatever the cluster's Secrets already hold, generates the values the suite
invents itself (the Settings admin password, the settings token, the Kubernetes module's agent
token) and leaves the rest empty to fill in. To change a secret, edit the file on the cluster
and run `sync`: it rewrites only the Secrets that changed and restarts only the workloads that
use them. The deploy tool runs `sync` itself.

## Deploying

Each part has a kustomize base in its `deploy/k8s`. What is particular to your cluster (the
namespace, how a part is exposed, where the modules are) goes in an overlay kept outside this
repository, and [`tools/deploy-k3s.sh`](tools/deploy-k3s.sh) builds, ships and applies a part
for a k3s cluster without a registry. [docs/deploying.md](docs/deploying.md) has the details and
a complete example overlay.

## Roadmap

Planned, not yet built:

1. **Finish moving the Kubernetes and Network modules onto the core.** They share its settings
   now; their collectors still duplicate its graph, tables and API code.
2. **A shared front end.** Each module carries its own copy of the 3D engine and panels; the
   common parts should move to `core/web`, with each module supplying its node kinds, tables
   and panels.
3. **Adding modules from the page.** The Settings page configures the modules the shell is
   told about, and turns each on or off. Next: adding a module by URL from the same page,
   instead of listing it in the shell's own settings.
4. **Links between modules.** The same machine is a VM on the ESXi map, a device on the
   network map and a node on the Kubernetes map; selecting it in one should lead to the others.
5. **More modules.** Any source that can be drawn as nodes and connections.

## Contributing and security

[CONTRIBUTING.md](CONTRIBUTING.md) says how to run each part and what a change needs.
Contributions come in under a [Contributor Licence Agreement](CLA.md), signed once on your first
pull request, and everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md). Security problems are reported privately: see
[SECURITY.md](SECURITY.md), which also sets out what the suite does and does not protect.

## Licence

Copyright (C) 2026 Troy Scafuro.

Workload Galaxy is free software: you can redistribute it and modify it under the terms of the
[GNU Affero General Public License, version 3](LICENSE) (`AGPL-3.0-only`; each source file says
so in its first lines). It comes with no warranty. If you run a
modified version as a service for others, the licence asks you to offer them its source.

Three.js, which the modules' front ends include, is under the MIT licence
(`web/vendor/THREE-LICENSE` in each module).

### Commercial licence

If the AGPL's terms do not fit how you want to use Workload Galaxy (for example, building it
into a product or a hosted service without publishing your changes), it can be licensed to you
under separate commercial terms. To ask, open an issue titled "Commercial licence" saying only
how to reach you, and you will be contacted.

### Name

"Workload Galaxy" and its logo are trademarks of Troy Scafuro. The licence covers the code, not
the name: a modified version you distribute or host needs a name of its own that cannot be
mistaken for this project.
