"""The module's settings, declared once for the suite's Settings page (see galaxy_core.settings).

`config.load_settings()` still reads the environment; what is saved from the Settings page is laid
over the environment before it runs (galaxy_core.settingsapi.boot), so both stay in step. Names
here are the environment variable's name without the NETGALAXY_ prefix, in lower case.
"""
from __future__ import annotations

from galaxy_core.settings import Field, schema

PREFIX = "NETGALAXY"
R, N, F, W, P, B, S, T = ("Router traffic (ntopng)", "Your network", "Firewall and DNS blocking", "Wi-Fi (UniFi controller)",
                          "Pings", "Blocking", "What cannot be observed", "Tuning")
FIELDS = [
    Field("mode", default="live", choices=("live", "sample"), label="Data source", group=R,
          help="live reads your router; sample shows a made-up network with one of each kind of finding."),
    Field("ntopng_url", label="ntopng address", group=R, required=True,
          help="Where ntopng runs on the router, e.g. https://192.168.1.1:3000."),
    Field("ntopng_user", label="ntopng user", group=R, required=True, help="A read-only ntopng login."),
    Field("ntopng_password", label="ntopng password", group=R, secret=True, required=True, bound_to=("ntopng_url", "ntopng_user"),
          help="Never shown again. Forgotten if the address or user changes, so it cannot be sent somewhere new by accident."),
    Field("ntopng_ifid", kind="int", default=0, minimum=0, label="ntopng interface number", group=R, advanced=True),
    Field("ntopng_verify_tls", kind="bool", default=False, label="Check ntopng's certificate", group=R, advanced=True,
          help="Off by default because the router's package serves a self-signed certificate."),

    Field("network_name", default="Home network", label="Name of the network", group=N),
    Field("router_ip", label="Router address", group=N, help="Its address on your network, e.g. 192.168.1.1."),
    Field("router_name", default="Router", label="Router name", group=N),
    Field("lan_cidrs", kind="list", default=["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"], label="Your address ranges", group=N,
          help="Comma-separated. Anything outside these counts as the internet."),
    Field("wan_interfaces", kind="list", label="Internet-facing interfaces", group=N,
          help="The router's WAN interface names, e.g. pppoe0. Empty counts every interface."),
    Field("vpn_cidrs", kind="list", label="VPN address ranges", group=N, help="Tunnel networks handed to VPN clients, e.g. 10.8.0.0/24."),
    Field("isp_gateway", label="ISP gateway", group=N, help="The first hop beyond the router, to tell a line fault from a router fault. Empty: not measured."),

    Field("sinkhole_ips", kind="list", label="DNS block page addresses", group=F,
          help="Where DNS blocking sends blocked names (pfBlockerNG's virtual address)."),
    Field("blocklist_trackers", kind="list", label="Known-bad rule numbers", group=F,
          help="Firewall rule tracker ids that mean a threat-list hit, so they are told apart from ordinary noise."),
    Field("syslog_allow", kind="list", label="Accept router logs from", group=F, advanced=True, help="Empty: the router address only."),
    Field("event_window_seconds", kind="int", default=3600, minimum=300, label="Keep firewall events for (seconds)", group=F, advanced=True),

    Field("unifi_url", label="Controller address", group=W, help="e.g. https://192.168.1.5:8443. Empty: no Wi-Fi details, latency only."),
    Field("unifi_user", label="Controller user", group=W, help="A local admin with the View Only role."),
    Field("unifi_password", label="Controller password", group=W, secret=True, bound_to=("unifi_url", "unifi_user"),
          help="Never shown again. Forgotten if the address or user changes."),
    Field("unifi_site", default="default", label="Controller site", group=W, advanced=True),
    Field("unifi_verify_tls", kind="bool", default=False, label="Check the controller's certificate", group=W, advanced=True),
    Field("unifi_seconds", kind="int", default=30, minimum=15, label="Read the controller every (seconds)", group=W, advanced=True),

    Field("ping", kind="bool", default=True, label="Measure latency and loss with pings", group=P),
    Field("ping_internet", kind="list", default=["1.1.1.1", "8.8.8.8"], label="Internet addresses to ping", group=P),
    Field("dns_probe_name", default="example.com", label="Name to look up when timing DNS", group=P, help="Empty turns the DNS timing off."),
    Field("ping_fast_seconds", kind="int", default=5, minimum=1, label="Ping the path every (seconds)", group=P, advanced=True),
    Field("ping_slow_seconds", kind="int", default=30, minimum=5, label="Ping other Wi-Fi devices every (seconds)", group=P, advanced=True),

    Field("admin_password", label="Blocking password", group=B, secret=True,
          help="Needed in the module to block or unblock an address. Empty: blocking is turned off."),
    Field("blocklist_token", label="Router token", group=B, secret=True, advanced=True,
          help="The token the router's sync script presents when it fetches the block list. Must match the one on the router."),
    Field("blocklist_senders", kind="list", label="Who may fetch the block list", group=B, advanced=True, help="Empty: the router address only."),
    Field("block_protect", kind="list", label="Never blockable", group=B, advanced=True,
          help="Addresses and ranges that can never be blocked, beyond your own ranges and the router."),

    Field("site", kind="json", label="Port forwards, device names and hosts", group=S,
          help='JSON: {"publicAddress": "", "forwards": [{"name", "port", "proto", "target", "targetPort"}], '
               '"devices": [{"ip" or "mac", "name", "type"}], "hosts": [{"id", "name", "role", "ips", "ouis"}]}. '
               "Wins over a site file."),

    Field("refresh_seconds", kind="int", default=30, minimum=10, label="Refresh every (seconds)", group=T, advanced=True),
    Field("flow_limit", kind="int", default=600, minimum=50, label="Busiest connections read per refresh", group=T, advanced=True),
    Field("detail_per_refresh", kind="int", default=6, minimum=0, label="Device lookups per refresh", group=T, advanced=True),
    Field("offline_hours", kind="int", default=24, minimum=0, label="Keep a device that left on the map for (hours)", group=T, advanced=True),
    Field("present_seconds", kind="int", default=900, minimum=60, label="A device is present if seen within (seconds)", group=T, advanced=True),
    Field("external_limit", kind="int", default=70, minimum=10, label="Internet services drawn individually", group=T, advanced=True),

    Field("syslog_port", kind="int", default=5514, label="Syslog port", group="Serving", editable=False),
    Field("config_file", kind="path", label="Site file", group="Serving", editable=False),
    Field("data_dir", kind="path", label="Data folder", group="Serving", editable=False),
    Field("settings_token", label="Settings token", group="Serving", secret=True, editable=False),
    Field("web_root", kind="path", label="UI folder", group="Serving", editable=False),
    Field("log_level", default="INFO", label="Log level", group="Serving", editable=False),
]

MANIFEST = {
    "id": "network", "title": "Network Galaxy",
    "summary": "A home or small-office network: devices, what they talk to, the firewall, Wi-Fi and VPN.",
    "needs": "A pfSense router running ntopng, reachable over HTTPS with a read-only login. Optional: remote syslog from the "
             "router, and a UniFi controller.",
    "envPrefix": PREFIX, "settings": schema(FIELDS, PREFIX),
}
