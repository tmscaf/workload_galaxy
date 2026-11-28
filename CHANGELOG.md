# Changelog

The suite as a whole. Each part keeps its own, with the detail:
[Kubernetes](modules/kubernetes/CHANGELOG.md) · [Network](modules/network/CHANGELOG.md) ·
[ESXi](modules/esxi/CHANGELOG.md).

## Unreleased

Suite bar 0.3.0, Network module 0.5.0.

- **Plugins.** A module can carry optional add-ons, each with its own switch and settings in
  Settings. The shared core has the frame; the Network module has the first one, an **ad
  blocker** that publishes one list of ad and tracker names for the router's DNS blocking to
  pull. See the [Network module's README](modules/network/README.md#plugins).
- **Defences on the Network map.** DNS blocking, the firewall and your block list are drawn as
  points, on a plane of their own in Strata, with a dashed line for whatever was stopped at each.
- **Modules can be turned on and off from Settings.** Each module has a switch. A module that is
  off leaves the drop-down and is no longer checked; it stays in Settings, where it can be set
  up and turned back on. `GALAXY_MODULES_OFF` sets which modules start off.
- The suite bar's Kubernetes base now includes a small volume, where those choices are kept.
- **A link back to the project.** The logo and name in the suite bar, and in each module's own
  top bar, open the project's page on GitHub, where the source and the licence are. The container
  images carry the same address and the licence as labels.

## 0.1.0

The first release of Workload Galaxy as a suite: Kubernetes module 0.12.0, Network module 0.3.0,
ESXi module 0.2.0, suite bar 0.2.1.

- **Three modules in one repository**, each its own service: Kubernetes and OpenShift, a home or
  small-office network behind a pfSense router, and a VMware ESXi host.
- **One bar for all of them.** The suite bar lists the modules in a drop-down with how many
  things need attention in each, and shows the one you pick underneath.
- **Settings.** Every module can be told what to read from one page behind one sign-in: forms
  are drawn from the settings each module declares, values can be tested before they are saved,
  and passwords are never shown back.
- **A shared core** for settings, the graph format, the tables and the API. The ESXi module is
  built on it; the Kubernetes and Network modules use it for their settings.
- **Secrets in one private file on the cluster**, turned into Kubernetes Secrets by
  `tools/cluster-secrets.sh`, so none are passed on a command line or kept in the repository.
- **Automated tests** for all five parts on every push.
- **Licensed under AGPL-3.0.**
