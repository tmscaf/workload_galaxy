# Changelog

## 0.5.0

- **Defences on the map.** DNS blocking, the firewall and your block list are drawn as points:
  on a new *Defences* plane in Strata, between the internet and your devices, and around the
  centre in Galaxy. Each appears only when there is data for it.
- **What was stopped is a line.** A device connecting to the block page, an outgoing attempt the
  firewall refused, the internet being turned away at the door, and a blocked service each get a
  dashed, red-grey line to the defence that stopped them, stronger the more was stopped. These
  carry a count, not a rate, and are kept out of the Connections table.
- Clicking a defence shows what it stopped and for whom. DNS blocking links to the ad blocker's
  page when that plugin is on.
- "Internet services in use" no longer has anything but internet services in it.

## 0.4.0

- **Plugins.** The module can carry optional add-ons, each with a switch and settings of its own
  under a heading in the suite's Settings page, off until turned on. A plugin can add findings, a
  page with its own tab, and API routes under `/api/v1/plugins/<id>`. The plugins are the ones in
  `collector/netgalaxy/plugins`; nothing else is loaded.
- **Ad blocker**, the first plugin. It fetches the block lists you name, keeps only plain host
  names, adds your own "always block" names, takes out your "never block" names and anything
  that belongs to a local network, and publishes one list for the router's DNS blocking to pull.
  Its page shows the lists and their state, when the router last fetched, your own names, and the
  devices being blocked now. No list is bundled, and the app still never connects to the router.
- A list that cannot be fetched keeps its last good copy across restarts, and the feed is held
  back rather than published short.
- New findings under Monitoring: a list that cannot be fetched, a setting that was left out, a
  full list, and a router that is not fetching.

## 0.3.0

- **Settings from the suite.** The router and ntopng login, your address ranges, the UniFi
  controller, pings, blocking and tuning can be changed from the suite's Settings page. Saving
  restarts the app in place, in a few seconds, with what was saved; nothing is redeployed. The
  device inventory, block list and history are kept.
- **Port forwards, device names and hosts as a setting.** What used to need a site file can be
  pasted into Settings as JSON; it is checked before it is saved and wins over the file.
- **Test connection** tries the ntopng login, and the UniFi controller if one is set, with what
  is typed, without saving it.
- **Passwords are write-only**, and a login's password is forgotten when its address or user
  changes, so it is never sent somewhere new by accident.
- **Changes need the settings token** (`NETGALAXY_SETTINGS_TOKEN`), which the suite shell
  presents. Without one the app takes its settings from its environment only, as before, and
  does not reveal them.
- The app now uses the suite's shared core for settings, so its image is built from the
  repository root (`docker build -f modules/network/Dockerfile .`).

## 0.2.0 — 2026-11-23

- **Wi-Fi tab: where does a connection break?** New section fed by a UniFi controller and by pings.
  - *The path every device shares*: the router, the ISP's gateway, the internet and DNS are pinged every
    5 s from the wired machine the app runs on; each shows typical round trip, loss, and its last hour.
    The first hop that answers late or not at all is named as the cause.
  - *Access points and switches*: state, link to the network (cable speed, or mesh), ping, and how busy
    each radio's channel is.
  - *Wi-Fi devices*, worst first: which access point and band, signal, share of frames re-sent, drops
    and moves in 24 h, ping time and loss, and a one-line verdict: weak signal, crowded or noisy air,
    the access point, keeps dropping, the device itself, or something upstream.
  - *One device*: its path hop by hop, 24 hours of ping history for the device, its access point, the
    router and the internet on one clock (who stopped answering first), signal history, its events,
    **Test now** (all hops pinged side by side for a few seconds) and **Watch closely** (ping every 5 s
    instead of every 30 s, and keep pinging while it is off the Wi-Fi).
- Wi-Fi findings in Triage (category "Wi-Fi & connection"), a Wi-Fi block in each device's details on
  the map, a Wi-Fi line in the network box, and names given to devices in the UniFi controller are used.
- New settings: `NETGALAXY_UNIFI_URL` / `_USER` / `_PASSWORD` / `_SITE`, `NETGALAXY_ISP_GATEWAY`,
  `NETGALAXY_PING*`, `NETGALAXY_DNS_PROBE_NAME`. Latency history (24 h) and Wi-Fi events are kept in
  `latency.json` and `wifi.json` in the data folder.

## 2026-11-20 (later 0.1.0 builds)

- **VPN connections**: a VPN tab (who is connected, from where, how long, data used; recent connects,
  disconnects and failed logins; scanners knocking on the VPN port) and a "VPN connections" group on
  the map with each session's traffic to home devices. Fed by the router's sync script (status every
  minute, `POST /api/v1/vpn-status`) and the forwarded VPN log. New setting `NETGALAXY_VPN_CIDRS`.
- A friendly name on the LAN through an Ingress, where the router's DNS has a host override for it.
- When ntopng stops answering (it can crash on a small router), the message now says so and
  where to start it again, instead of a bare `ConnectTimeout`.
- Flow requests sort by `bytes` (ntopng adds the `column_` prefix itself; the old value made it log
  a warning on every refresh).

## 0.1.0 — 2026-11-19

First version.

- Live map of the network from ntopng on a pfSense router: devices grouped by kind, the internet
  services they use, traffic between them, and what is reachable from outside through port forwards.
- Device inventory with names (site file, DHCP, what the device announces), manufacturer, type,
  and a per-device picture of normal traffic that survives restarts.
- Findings: known-bad contact, unusual upload or fan-out, admin services open to the internet,
  forwards pointing at machines that are off, firewall blocks and port scans, blocked DNS lookups,
  new and unidentified devices, and the health of the monitoring itself.
- Router syslog receiver (firewall and DHCP events) and reachability checks for quiet servers.
- Front end adapted from Workload Galaxy 0.11.0 (Galaxy and Strata views, Triage, search).
- Blocking: Block buttons on findings and bare-address services, an action password, and a block list the
  router pulls once a minute and re-validates (pfBlockerNG list NetGalaxy, both directions).
- `sample` mode with a made-up network, and a test suite.
