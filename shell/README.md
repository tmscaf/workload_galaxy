# Workload Galaxy shell

The suite's front door: one bar across the top, and the selected module underneath.

```
┌──────────────────────────────────────────────────────────────────────────┐
│ Workload Galaxy │ ● Kubernetes 13 ▾ │ demo-ocp          v0.11.0 · Open ↗ │
├─────────────────┴───────────────────┴────────────────────────────────────┤
│                         the selected module                              │
```

- **Workload Galaxy** on the left is fixed.
- **The module selector** is a drop-down. Each entry shows the module, what it is watching, and
  how it is doing: how many things need attention, how many of those are critical, or that it
  cannot be reached.
- **The rest of the bar** describes the module in view: what it is watching, its version,
  whether it is on sample data, and a link that opens it on its own.

- **Settings**, at the right end, opens the settings area in place of the module.

The module in view is kept in the address (`#esxi`), so a link or a reload comes back to it, and
is remembered for the next visit. The selector works with the keyboard: arrow keys, Enter, Esc.

## Settings

The settings area is where each module is told what to read: its host, its account, how things
are grouped. Each module's form is drawn from what the module itself says it takes, so a new
module needs no page of its own.

- **One sign-in.** The area is behind the shell's admin password. Viewing the modules needs none.
  Five wrong passwords from one address lock sign-in for five minutes.
- **Test connection** tries what is typed without saving it.
- **Save and apply** hands the settings to the module, which keeps them in its own data folder
  and starts using them: ESXi at once, Kubernetes and Network after restarting themselves, which
  takes a few seconds. The shell stores none of it.
- **Passwords are write-only.** The page is told whether one is set, never what it is.
- **Each value says where it comes from:** saved here, set where the module is deployed, or
  its default. Saved values win.

It takes two secrets to turn on:

| | On the shell | On each module |
|---|---|---|
| Admin password | `GALAXY_ADMIN_PASSWORD` | |
| Settings token (the same value everywhere) | `GALAXY_SETTINGS_TOKEN` | `GALAXY_SETTINGS_TOKEN` (Kubernetes), `NETGALAXY_SETTINGS_TOKEN`, `ESXIGALAXY_SETTINGS_TOKEN` |

The token is what a module trusts: it accepts settings only from a caller that presents it, and
a browser never sees it. Without an admin password the area is off; without a token, settings
cannot be read or changed from the suite and every module keeps what it was deployed with.

A module without a settings API (an older version, say) is listed as configured where it is
deployed.

## How it works

Each module is its own service; the shell does not run them. It is told where they are, shows
the one you pick in a frame through the module's embed mode (`?embed=1`), and asks each one how
it is doing with two small reads of its public API (`/api/v1/meta` and `/api/v1/triage`), every
20 seconds. The shell's server does the asking, because a page may not read another site's API.

It keeps no state. Its only secrets are the two that turn the settings area on; a module's own
credentials pass through it on the way to the module and are not kept.

## Run it

Start the modules you want (see the [suite README](../README.md#try-it) for running each on
made-up data), then:

```bash
cd shell
python3 -m venv .venv && .venv/bin/pip install -r app/requirements.txt
cd app
PYTHONPATH=../../core/py GALAXY_MODULES="kubernetes=http://127.0.0.1:8099,network=http://127.0.0.1:8098,esxi=http://127.0.0.1:8097" \
  ../.venv/bin/uvicorn galaxyshell.main:app --port 8089
# open http://127.0.0.1:8089/
```

Or as a container, built from the repository root:

```bash
docker build -f shell/Dockerfile -t workload-galaxy-shell .
docker run --rm -p 8089:8080 --read-only -e GALAXY_MODULES="esxi=http://esxi-galaxy.example:8080" workload-galaxy-shell
```

`deploy/k8s` is the Kubernetes base and `deploy/homelab` an overlay for one home lab;
`scripts/deploy-homelab.sh` builds, ships and applies it. The admin password and the settings
token come from the cluster's private secrets file (see [Secrets](../README.md#secrets)).

## Configuration

| Variable | Default | What it is |
|---|---|---|
| `GALAXY_MODULES` | | Comma-separated `id=URL` pairs, in the order they should be listed. The URL is the one a browser uses |
| `GALAXY_MODULES_FILE` | | A JSON file instead, when a module needs its own title or a separate address for the shell to check it on: `{"modules": [{"id", "title", "url", "probeUrl"}]}` |
| `GALAXY_ADMIN_PASSWORD` | | Opens the settings area. Empty: the area is off |
| `GALAXY_SETTINGS_TOKEN` | | Presented to modules to read and change their settings. Each module must be given the same value |
| `GALAXY_PROBE_SECONDS` | 20 | How often each module is asked how it is doing |
| `GALAXY_PROBE_VERIFY_TLS` | off | Check modules' certificates when asking |
| `GALAXY_WEB_ROOT` | | Folder the page is served from (set by the image) |
| `GALAXY_LOG_LEVEL` | INFO | Log level |

The ids `kubernetes`, `network` and `esxi` get their names and one-line descriptions
automatically. Any other id is listed under its own name.

## Limits

- **A module's address must be reachable from the browser**, not only from the shell: the frame
  loads it directly.
- **A page served over HTTPS cannot frame a module served over plain HTTP.** Serve both the same
  way.
- **What you are looking at inside a module is not in the address.** The address remembers which
  module, not which view or selection within it. "Open on its own" gives a module its full
  address bar back.
- **Modules cannot be switched on or added from the page yet.** The list comes from the shell's
  own settings; the settings area configures the modules in it.
- **Viewing has no sign-in**, here or in the modules. Only the settings area does.
- **Sign-ins end when the shell restarts**, and the admin password travels in the clear unless
  the shell is served over HTTPS.

Tests: `.venv/bin/pip install pytest httpx` once, then `.venv/bin/python -m pytest -q tests`
from this directory.
