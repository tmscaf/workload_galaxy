# Changelog

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
- Friendly LAN name: `http://netgalaxy` / `http://netgalaxy.home.arpa` via a Traefik Ingress
  (needs the pfSense DNS Resolver Host Override `netgalaxy.home.arpa -> 192.168.1.20`).
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
