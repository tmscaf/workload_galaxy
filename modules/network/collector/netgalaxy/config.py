# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""Runtime settings, read from environment variables (see README for the full table)."""
from __future__ import annotations

import ipaddress
import json
import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    mode: str = "live"                         # "live" (ntopng + syslog) or "sample" (built-in sample network)
    network_name: str = "Home network"
    refresh_seconds: int = 30
    # ntopng (runs on the router): the traffic source
    ntopng_url: str = ""
    ntopng_user: str = ""
    ntopng_password: str = ""
    ntopng_ifid: int = 0
    ntopng_verify_tls: bool = False            # the pfSense package serves a self-signed certificate
    flow_limit: int = 600                      # busiest flows read per refresh (listing them is slow on small routers)
    detail_per_refresh: int = 6                # per-device detail lookups per refresh (MAC, device type)
    # syslog from the router: firewall and DHCP events
    syslog_port: int = 5514                    # UDP; 0 disables the receiver
    syslog_allow: list[str] = field(default_factory=list)       # only these senders are accepted; empty = router_ip
    event_window_seconds: int = 3600
    # the network itself
    lan_cidrs: list[str] = field(default_factory=lambda: ["192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12"])
    router_ip: str = ""
    router_name: str = "Router"
    wan_interfaces: list[str] = field(default_factory=list)     # e.g. pppoe0; empty = every interface counts
    sinkhole_ips: list[str] = field(default_factory=list)       # where DNS blocking sends blocked names (pfBlockerNG VIP)
    blocklist_trackers: list[str] = field(default_factory=list) # firewall rule ids that mean "known-bad address"
    offline_hours: int = 24                    # how long a device that left stays on the map, dimmed
    present_seconds: int = 900                 # seen this recently = present
    external_limit: int = 70                   # internet destinations drawn individually; the rest are grouped
    config_file: str = ""                      # JSON: port forwards, device names, hosts (see the README)
    # blocking (the router pulls the list; the app holds no router credentials)
    admin_password: str = ""                   # needed to block/unblock; empty = actions disabled
    blocklist_token: str = ""                  # the router's sync script presents this; empty = list not served
    blocklist_senders: list[str] = field(default_factory=list)  # who may fetch it; empty = router_ip
    block_protect: list[str] = field(default_factory=list)      # never blockable, beyond the LAN and router
    data_dir: str = ""                         # where the device inventory is kept between restarts
    # VPN (the router's sync script reports who is connected; its VPN log arrives over syslog)
    vpn_cidrs: list[str] = field(default_factory=list)          # tunnel networks handed to VPN clients, e.g. 10.8.0.0/24
    # Wi-Fi and connection health (a UniFi controller for the Wi-Fi half; pings for latency and loss)
    unifi_url: str = ""                        # e.g. https://192.168.1.5:8443; empty = no Wi-Fi data
    unifi_user: str = ""
    unifi_password: str = ""
    unifi_site: str = "default"
    unifi_verify_tls: bool = False             # controllers serve a self-signed certificate
    unifi_seconds: int = 30                    # how often the controller is read
    ping: bool = True                          # measure latency and loss with ICMP echo
    ping_fast_seconds: int = 5                 # router, ISP gateway, internet, access points, watched devices
    ping_slow_seconds: int = 30                # every other Wi-Fi device
    ping_internet: list[str] = field(default_factory=lambda: ["1.1.1.1", "8.8.8.8"])
    isp_gateway: str = ""                      # the first hop beyond the router; empty = not measured
    dns_probe_name: str = "example.com"        # looked up through the router to time DNS; empty = off
    # serving
    web_root: str = "/app/web"
    log_level: str = "INFO"
    detail_limit: int = 400                    # a home network is always drawn device by device
    project_limit: int = 60
    node_budget: int = 600
    site: dict = field(default_factory=dict)   # contents of config_file

    def is_lan(self, ip: str) -> bool:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if a.is_link_local or a.is_multicast or a.is_loopback or a.is_unspecified:
            return True        # never "the internet"
        return any(a in n for n in self._nets())

    def is_vpn(self, ip: str) -> bool:
        """An address handed to a VPN client (it is drawn as that VPN session, never as a device)."""
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return False
        nets = getattr(self, "_vpn_cache", None)
        if nets is None:
            nets = []
            for c in self.vpn_cidrs:
                try:
                    nets.append(ipaddress.ip_network(c, strict=False))
                except ValueError:
                    log.warning("ignoring bad VPN CIDR %r", c)
            object.__setattr__(self, "_vpn_cache", nets)
        return any(a in n for n in nets)

    def _nets(self):
        nets = getattr(self, "_net_cache", None)
        if nets is None:
            nets = []
            for c in self.lan_cidrs:
                try:
                    nets.append(ipaddress.ip_network(c, strict=False))
                except ValueError:
                    log.warning("ignoring bad CIDR %r", c)
            object.__setattr__(self, "_net_cache", nets)
        return nets


def load_site(path: str) -> dict:
    """The site file: what cannot be observed. Missing or broken means 'none', never a crash."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as err:
        log.warning("cannot read %s: %s", path, err)
        return {}


def load_settings() -> Settings:
    here = os.path.dirname(os.path.abspath(__file__))
    local_web = os.path.normpath(os.path.join(here, "..", "..", "web"))
    s = Settings(
        mode=os.environ.get("NETGALAXY_MODE", "live").strip().lower() or "live",
        network_name=os.environ.get("NETGALAXY_NETWORK_NAME", "Home network"),
        refresh_seconds=max(10, _int("NETGALAXY_REFRESH_SECONDS", 30)),
        ntopng_url=os.environ.get("NETGALAXY_NTOPNG_URL", "").rstrip("/"),
        ntopng_user=os.environ.get("NETGALAXY_NTOPNG_USER", ""),
        ntopng_password=os.environ.get("NETGALAXY_NTOPNG_PASSWORD", ""),
        ntopng_ifid=_int("NETGALAXY_NTOPNG_IFID", 0),
        ntopng_verify_tls=_bool("NETGALAXY_NTOPNG_VERIFY_TLS", False),
        flow_limit=max(50, _int("NETGALAXY_FLOW_LIMIT", 600)),
        detail_per_refresh=max(0, _int("NETGALAXY_DETAIL_PER_REFRESH", 6)),
        syslog_port=_int("NETGALAXY_SYSLOG_PORT", 5514),
        syslog_allow=_list("NETGALAXY_SYSLOG_ALLOW"),
        event_window_seconds=max(300, _int("NETGALAXY_EVENT_WINDOW_SECONDS", 3600)),
        lan_cidrs=_list("NETGALAXY_LAN_CIDRS", "192.168.0.0/16,10.0.0.0/8,172.16.0.0/12"),
        router_ip=os.environ.get("NETGALAXY_ROUTER_IP", ""),
        router_name=os.environ.get("NETGALAXY_ROUTER_NAME", "Router"),
        wan_interfaces=_list("NETGALAXY_WAN_INTERFACES"),
        sinkhole_ips=_list("NETGALAXY_SINKHOLE_IPS"),
        blocklist_trackers=_list("NETGALAXY_BLOCKLIST_TRACKERS"),
        offline_hours=max(0, _int("NETGALAXY_OFFLINE_HOURS", 24)),
        present_seconds=max(60, _int("NETGALAXY_PRESENT_SECONDS", 900)),
        external_limit=max(10, _int("NETGALAXY_EXTERNAL_LIMIT", 70)),
        config_file=os.environ.get("NETGALAXY_CONFIG_FILE", ""),
        admin_password=os.environ.get("NETGALAXY_ADMIN_PASSWORD", ""),
        blocklist_token=os.environ.get("NETGALAXY_BLOCKLIST_TOKEN", ""),
        blocklist_senders=_list("NETGALAXY_BLOCKLIST_SENDERS"),
        block_protect=_list("NETGALAXY_BLOCK_PROTECT"),
        data_dir=os.environ.get("NETGALAXY_DATA_DIR", ""),
        vpn_cidrs=_list("NETGALAXY_VPN_CIDRS"),
        unifi_url=os.environ.get("NETGALAXY_UNIFI_URL", "").rstrip("/"),
        unifi_user=os.environ.get("NETGALAXY_UNIFI_USER", ""),
        unifi_password=os.environ.get("NETGALAXY_UNIFI_PASSWORD", ""),
        unifi_site=os.environ.get("NETGALAXY_UNIFI_SITE", "default") or "default",
        unifi_verify_tls=_bool("NETGALAXY_UNIFI_VERIFY_TLS", False),
        unifi_seconds=max(15, _int("NETGALAXY_UNIFI_SECONDS", 30)),
        ping=_bool("NETGALAXY_PING", True),
        ping_fast_seconds=max(1, _int("NETGALAXY_PING_FAST_SECONDS", 5)),
        ping_slow_seconds=max(5, _int("NETGALAXY_PING_SLOW_SECONDS", 30)),
        ping_internet=_list("NETGALAXY_PING_INTERNET", "1.1.1.1,8.8.8.8"),
        isp_gateway=os.environ.get("NETGALAXY_ISP_GATEWAY", "").strip(),
        dns_probe_name=os.environ.get("NETGALAXY_DNS_PROBE_NAME", "example.com").strip(),
        web_root=os.environ.get("NETGALAXY_WEB_ROOT", local_web if os.path.isdir(local_web) else "/app/web"),
        log_level=os.environ.get("NETGALAXY_LOG_LEVEL", "INFO").upper(),
    )
    s.site = load_site(s.config_file)
    # The same thing saved from the suite's Settings page wins over the file.
    saved_site = os.environ.get("NETGALAXY_SITE", "").strip()
    if saved_site:
        try:
            data = json.loads(saved_site)
            if isinstance(data, dict):
                s.site = data
        except ValueError as err:
            log.warning("ignoring the saved site details (not valid JSON): %s", err)
    if not s.syslog_allow and s.router_ip:
        s.syslog_allow = [s.router_ip]
    if not s.blocklist_senders and s.router_ip:
        s.blocklist_senders = [s.router_ip]
    return s
