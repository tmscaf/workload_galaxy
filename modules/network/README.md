# Network Galaxy

A live map of a home or small-office network: every device, what each one is talking to on the
internet, what the firewall is turning away, and anything that looks abnormal.

It is a sibling of the [Kubernetes module](../kubernetes): the same 3D front end and the
same graph format, fed by a pfSense router instead of a Kubernetes cluster.

| On the map | Means |
|---|---|
| A star | A device on your network, grouped by kind (servers, computers, phones & tablets, TV & media, smart home, printers, network gear, unidentified) |
| A point beyond the gateway | An internet service your devices use, grouped by service ("Netflix", "github.com"), not by address |
| A line | Traffic; brighter means more of it |
| "The internet (inbound)" | What the outside can reach through your port forwards |
| Triage | Findings in plain words: what was seen, why it matters, what to do |
| Wi-Fi | Where a connection breaks: latency and loss hop by hop, access points, every Wi-Fi device's signal and drops |

## What it reports

| Category | Examples |
|---|---|
| Known-bad contact | A device with open connections to an address on a threat list; an attempt to reach one that the firewall stopped |
| Unusual behaviour | A device uploading far more than its own normal; contacting far more hosts than usual; a high ntopng behaviour score |
| Open to the internet | Every port forward; admin services (SSH, databases, remote desktop, Webmin) open to everyone; forwards pointing at a machine that is off |
| Turned away | Connection attempts the firewall dropped, the busiest sources and ports, port scans |
| Blocked lookups | Devices that keep asking for names on the DNS block list |
| Wi-Fi & connection | The internet line, router or DNS slow or dropping pings; an access point offline, on a crowded channel, mesh-linked or on a slow cable; a device with a weak signal, a noisy link, or that keeps dropping |
| Devices | Devices that joined in the last day; devices that could not be identified |
| Monitoring | ntopng unreachable; router logs not arriving; UniFi controller unreadable; pings not running |

"Unusual" is judged against each device's own history, so it needs about an hour of readings
before it says anything, and nothing is called "new" during the first day.

## Where the data comes from

| Source | What it gives | How |
|---|---|---|
| ntopng on the router | Devices, their traffic rates, who talks to whom, application names, threat-list flags | REST API, polled every 30 s with a login that has no admin rights |
| Router syslog | Firewall blocks, DHCP leases (address ↔ MAC ↔ the name a device gave itself) | The router sends UDP syslog to this app |
| Reachability checks | Whether a quiet server is still up | One TCP connection, opened and closed, to each port-forward target and each server named in the site file, every 2 minutes |
| UniFi controller | Access points and switches, which access point each Wi-Fi device is on, signal, re-sent frames, channel use, joins and drops | Its API, read every 30 s with a View Only login |
| Pings | Round-trip time and loss to the router, the ISP gateway, the internet, each access point and each Wi-Fi device; one DNS lookup through the router | ICMP echo from the app: every 5 s for the shared path, access points and watched devices, every 30 s for other Wi-Fi devices |
| Site file (JSON) | What cannot be observed: port forwards, names for your servers, which machines are VMs on which host | `deploy/homelab/site.json`, mounted read-only |

The app never connects to the router to change it. The one thing it can influence is the list of
addresses you chose to block, which the router pulls (see Blocking).

## Blocking

Findings about a known-bad address or a port scan, and internet services that are a bare address,
get a **Block** button. Blocking needs the action password; viewing does not.

```
you click Block (reason, 1 day / 7 days / 30 days / permanent)
  -> the app adds the address to its list (data volume, survives restarts)
  -> every minute the router runs /root/netgalaxy/netgalaxy-sync.php: it fetches the list
     (token + the router's own address are checked), validates every line again, and loads what
     passes into the pfBlockerNG table pfB_NetGalaxy_v4, blocked in both directions
  -> on its next fetch it reports the hash of what it loaded, so the app shows "on the router since ..."
```

What can never be blocked, checked by the app and again by the router: anything that is not a
public IPv4 address, ranges wider than /24, your LAN, the router's own addresses and gateways, the
DNS servers in `NETGALAXY_BLOCK_PROTECT` and `/root/netgalaxy/protect`, and more than 2000 entries.
The app holds no router credentials, so the most a compromised app can do is add internet
addresses to that one list.

The pfBlockerNG list `NetGalaxy` (Deny Both, local feed `/var/db/pfblockerng/netgalaxy.txt`) keeps
the table across filter reloads and reboots. Its feed always contains `192.0.2.1`, a documentation
address, because pfBlockerNG removes a list's rules while the list is empty.

Router setup (once): copy `deploy/router/netgalaxy-sync.php` to `/root/netgalaxy/`, put the list's
URL in `/root/netgalaxy/url` and the token in `/root/netgalaxy/token` (mode 600), add the pfBlockerNG
list above, and add a cron entry `* * * * * root /usr/local/bin/php -q /root/netgalaxy/netgalaxy-sync.php`.
App setup: Secret `network-galaxy-actions` with `admin-password` and `blocklist-token`.

## VPN connections

The **VPN** tab lists who is connected to the router's OpenVPN server right now (user, device, where
they connect from, tunnel address, how long, data used) and recent connects, disconnects and failed
logins. On the map each session is a node in the **VPN connections** group, hanging off the router,
with lines to the home devices it uses (what ntopng sees from its tunnel address).

Two feeds, both started by the router:

- **Status, once a minute**: the same sync script that pulls the block list also posts the VPN
  server's client list (pfSense's `openvpn_get_active_servers()`) to `/api/v1/vpn-status`, with the
  same token and sender check. This decides who is "connected now".
- **The VPN log**: remote logging's *VPN Events* forwards OpenVPN's log over the syslog feed, so
  connects and disconnects show within seconds, and failed logins and scanners knocking on the VPN
  port are counted.

Nothing here can disconnect anyone or change the VPN. Set `NETGALAXY_VPN_CIDRS` to the tunnel
network so tunnel addresses are drawn as VPN sessions, not as devices. Sessions and the last 300
events are kept in `vpn.json` in the data folder.

## Wi-Fi: where does a connection break?

A Wi-Fi device reaches the internet over a chain, and each link fails in its own way:

```
device ~~ radio link ~~ access point -- cable or mesh -- router -- ISP gateway -- the internet
```

The **Wi-Fi** tab measures every link of that chain and names the first one that looks wrong.

| It says | Because |
|---|---|
| Internet line / Internet | The router answers but the ISP's gateway or the internet beyond it is slow or losing pings |
| Router | The router itself answers late or drops pings on the wired network |
| Name lookups | The connection is fine but the router's DNS resolver is slow or failing |
| Access point | The access point is offline, loses pings on its wired side, is mesh-linked, or its cable linked below 1 Gbps |
| Crowded or noisy air | The channel is 70%+ busy, or 20%+ of frames to the device are re-sent although its signal is strong |
| Weak signal | The device's signal is below -72 dBm (very weak below -80) |
| Keeps dropping | It fell off the Wi-Fi and came back 3+ times in an hour or 6+ in a day, or hops between access points |
| The device itself | Everything above is healthy, yet the device answers pings poorly while in use |

Open a device for its path hop by hop, 24 hours of history on one clock for the device, its access
point, the router and the internet (who stopped answering first), its signal over time and its events.
**Test now** pings all its hops side by side for a few seconds; **Watch closely** pings it every 5 s
instead of every 30 s and keeps pinging while it is off the Wi-Fi.

Things to know:

- Pings come from where the app runs: a wired machine. A healthy router and access point with a
  struggling device therefore points at the radio link between them.
- Phones and tablets sleep and ignore pings while idle. Loss on an idle or power-saving device is not
  counted against it; its signal, re-sent frames and drops are.
- Drops come from the controller's own event list when it serves one, and are otherwise worked out
  from the change between two readings (a device that re-joins between readings still shows, because
  its association time restarts).
- Nothing is changed on the controller: the app signs in and reads. A **View Only** admin is enough
  (UniFi: Settings > Admins > add a local admin with the role View Only).

Setup: `NETGALAXY_UNIFI_URL` plus the login (on the home lab: the Secret `network-galaxy-unifi`, keys
`user` and `password`, which `UNIFI_USER=... UNIFI_PASSWORD=... scripts/deploy-homelab.sh` creates),
and `NETGALAXY_ISP_GATEWAY` for the first hop beyond the router. On Kubernetes the pod needs the
sysctl `net.ipv4.ping_group_range` (it is in `deploy/k8s`); no capability or root is needed.

## Run it without a router

```bash
python3 -m venv .venv && .venv/bin/pip install -r collector/requirements.txt pytest
cd collector
NETGALAXY_MODE=sample NETGALAXY_SYSLOG_PORT=0 NETGALAXY_SINKHOLE_IPS=10.99.0.1 \
  NETGALAXY_ROUTER_IP=10.20.0.1 NETGALAXY_LAN_CIDRS=10.20.0.0/24 \
  ../.venv/bin/uvicorn netgalaxy.main:app --port 8098
# open http://127.0.0.1:8098/
```

`sample` mode serves a made-up network (16 devices) with one of each kind of finding.
Tests: `cd collector && ../.venv/bin/python -m pytest -q ../tests`.

## Set up the router (pfSense)

1. **ntopng** (Diagnostics > ntopng Settings): enabled on the LAN interface.
2. **A read-only ntopng user** for this app: in ntopng, Settings > Users > add a user with the
   role "Non Privileged User". Its name and password go into the Kubernetes Secret (below).
3. **Remote logging** (Status > System Logs > Settings): tick *Enable Remote Logging*, set a remote
   log server to `<this app's address>:5514`, and tick *Firewall Events*, *DHCP Events* and *VPN Events*.
4. **pfBlockerNG** (optional): put its DNS-block address in `NETGALAXY_SINKHOLE_IPS` and the rule id
   of its IP block rule (the number after `id:` in `pfctl -sr | grep pfB_`) in
   `NETGALAXY_BLOCKLIST_TRACKERS`, so known-bad hits are told apart from ordinary noise.

## Deploy to the home lab

```bash
NTOPNG_USER=<user> NTOPNG_PASSWORD=<password> scripts/deploy-homelab.sh   # first time
scripts/deploy-homelab.sh                                                # afterwards
```

The script builds the image for amd64, ships it to the k3s node, creates the Rancher project and
namespace `network-galaxy` on first use, stores the ntopng login in the Secret
`network-galaxy-ntopng`, and applies `deploy/homelab`. The UI is at `http://192.168.1.20:8091`;
syslog is received on UDP 5514. The device inventory lives on a small volume so history survives
restarts.

## The site file

```json
{
  "forwards": [{"name": "Game server", "port": "27015", "proto": "udp", "target": "192.168.1.20", "targetPort": "27015"}],
  "devices":  [{"ip": "192.168.1.20", "name": "game-server", "type": "server"},
               {"mac": "AA:BB:CC:00:11:22", "name": "Kitchen display", "type": "media"}],
  "hosts":    [{"id": "esxi", "name": "ESXi host", "ouis": ["00:0C:29"], "ips": ["192.168.1.5"]}]
}
```

- `forwards` mirrors the router's NAT port forwards (and sites published through a reverse proxy).
  Keep it in step with the router by hand: the app cannot read the router's configuration.
- `devices` names a device by MAC (preferred, it survives address changes) or IP. Types: `server`,
  `vm`, `nas`, `computer`, `laptop`, `phone`, `tablet`, `tv`, `media`, `console`, `iot`, `camera`,
  `printer`, `network`, `wifi`.
- `hosts` says which machines run VMs; a device whose MAC starts with one of the `ouis` is drawn on that host.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `NETGALAXY_MODE` | `live` | `live`, or `sample` for the built-in sample network |
| `NETGALAXY_NETWORK_NAME` | `Home network` | Name shown in the UI |
| `NETGALAXY_NTOPNG_URL` / `_USER` / `_PASSWORD` | | ntopng address and read-only login |
| `NETGALAXY_NTOPNG_IFID` | `0` | ntopng interface id (the LAN interface) |
| `NETGALAXY_NTOPNG_VERIFY_TLS` | `false` | The pfSense package serves a self-signed certificate |
| `NETGALAXY_REFRESH_SECONDS` | `30` | How often ntopng is read |
| `NETGALAXY_FLOW_LIMIT` | `600` | Busiest flows read per refresh (listing flows is slow on small routers) |
| `NETGALAXY_SYSLOG_PORT` | `5514` | UDP port for the router's syslog; `0` turns the receiver off |
| `NETGALAXY_SYSLOG_ALLOW` | router IP | Senders whose syslog is accepted |
| `NETGALAXY_LAN_CIDRS` | private ranges | What counts as "your network" |
| `NETGALAXY_ROUTER_IP` / `_NAME` | | The router's LAN address and display name |
| `NETGALAXY_WAN_INTERFACES` | all | Router interfaces that face the internet (e.g. `pppoe0`) |
| `NETGALAXY_SINKHOLE_IPS` | | Where DNS blocking sends blocked names |
| `NETGALAXY_BLOCKLIST_TRACKERS` | | Firewall rule ids that mean "known-bad address" |
| `NETGALAXY_OFFLINE_HOURS` | `24` | How long a device that left stays on the map, dimmed |
| `NETGALAXY_EXTERNAL_LIMIT` | `70` | Internet services drawn one by one; the rest are folded into one node |
| `NETGALAXY_CONFIG_FILE` | | Path of the site file |
| `NETGALAXY_DATA_DIR` | | Where the device inventory is kept; empty = memory only |
| `NETGALAXY_VPN_CIDRS` | | Tunnel networks handed to VPN clients (e.g. `10.8.0.0/24`): drawn as VPN sessions |
| `NETGALAXY_UNIFI_URL` / `_USER` / `_PASSWORD` | | UniFi controller address (e.g. `https://192.168.1.5:8443`) and a View Only login; empty = no Wi-Fi data |
| `NETGALAXY_UNIFI_SITE` | `default` | The controller's site id |
| `NETGALAXY_UNIFI_SECONDS` | `30` | How often the controller is read |
| `NETGALAXY_PING` | `true` | Measure latency and loss with pings |
| `NETGALAXY_PING_FAST_SECONDS` / `_SLOW_SECONDS` | `5` / `30` | Ping interval for the shared path, access points and watched devices / for other Wi-Fi devices |
| `NETGALAXY_PING_INTERNET` | `1.1.1.1,8.8.8.8` | Public addresses that stand for "the internet" (it is as good as the best of them) |
| `NETGALAXY_ISP_GATEWAY` | | The first hop beyond the router; empty = not measured |
| `NETGALAXY_DNS_PROBE_NAME` | `example.com` | Name looked up through the router to time DNS; empty = off |
| `NETGALAXY_ADMIN_PASSWORD` | | Password for blocking; empty = watch-only |
| `NETGALAXY_BLOCKLIST_TOKEN` | | Token the router presents to fetch the block list; empty = list not served |
| `NETGALAXY_BLOCKLIST_SENDERS` | router IP | Addresses allowed to fetch the block list |
| `NETGALAXY_BLOCK_PROTECT` | | Extra addresses or ranges that can never be blocked (gateway, DNS servers) |

## Limits

- **Encrypted traffic:** it sees who a device talks to and how much, never the content.
- **Flows are sampled by size:** only the busiest flows are read each refresh, so a device's list of
  destinations is its heaviest ones, not all of them. Upload and download rates per device are exact.
- **Presence:** a device is "online" while ntopng has seen it in the last 15 minutes. A sleeping
  phone drops off and comes back; servers named in the site file are checked directly instead.
- **Private Wi-Fi addresses:** phones that rotate their MAC address appear as a new device each time.
- **History:** firewall events are a rolling hour in memory; baselines are a slow average per
  device, not a time series. For long-term graphs use ntopng itself.
- **Blocked lookups** are counted from connections to the block page, not read from pfBlockerNG's
  own log, so the names are not shown (pfBlockerNG > Reports has them).

## Layout

```
collector/netgalaxy/   ntopng.py (client), syslog.py (receiver + parsers), devices.py (inventory),
                       blocks.py (block list), auth.py (action password),
                       probes.py (reachability), graph.py (builder), findings.py (checks),
                       pinger.py (latency and loss), unifi.py (controller client), wifi.py (where a connection breaks),
                       state.py (refresh loop), main.py (API), sample.py (sample network),
                       scale.py + graphops.py (shared with the Kubernetes module)
web/                   the front end (Three.js, no build step)
deploy/k8s, deploy/homelab, deploy/router (pfSense sync script), scripts/deploy-homelab.sh
tests/
```
