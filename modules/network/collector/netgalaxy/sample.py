"""A made-up network for trying the app without a router (NETGALAXY_MODE=sample) and for the tests.

It answers the same questions as the live sources, in the same shapes: ntopng's host and flow lists,
the manufacturer table, DHCP leases, and a few firewall log lines. Addresses are from documentation
ranges; nothing here describes a real network.
"""
from __future__ import annotations

import math
import zlib

LAN = "10.20.0."
VPN_TUNNEL = "10.30.0.2"            # the address the VPN server handed to the sample VPN client
DEVICES = [   # last octet, name ntopng saw, MAC, manufacturer, ntopng type, down bytes/s, up bytes/s, DHCP name
    (10, "nas", "00:11:32:AA:00:10", "Synology Incorporated", 10, 40_000, 220_000, "nas"),
    (11, "10.20.0.11", "00:0C:29:AA:00:11", "VMware, Inc.", 3, 15_000, 90_000, "game-server"),
    (12, "10.20.0.12", "00:0C:29:AA:00:12", "VMware, Inc.", 3, 6_000, 2_500, "web"),
    (20, "studio-mac", "A4:83:E7:AA:00:20", "Apple, Inc.", 3, 900_000, 60_000, "studio-mac"),
    (21, "thinkpad-x1", "8C:16:45:AA:00:21", "LCFC(HeFei) Electronics", 4, 120_000, 18_000, "thinkpad-x1"),
    (30, "ipad", "DE:C8:04:AA:00:30", "", None, 2_400_000, 40_000, "Sams-iPad"),
    (31, "0f2b7c1e-5a1d-4d0e-9a53-1c2d3e4f5a6b", "B2:9E:C2:AA:00:31", "", None, 60_000, 9_000, ""),
    (32, "android-5c1f0e", "7E:B7:71:AA:00:32", "", None, 30_000, 2_600_000, "android-5c1f0e"),
    (40, "living room tv", "C4:DD:57:AA:00:40", "Samsung Electronics Co.,Ltd", 7, 3_100_000, 30_000, "LivingRoomTV"),
    (41, "spotifyconnect", "18:7F:88:AA:00:41", "Sonos, Inc.", 11, 45_000, 2_000, ""),
    (50, "ring-doorbell", "4E:96:09:AA:00:50", "Ring LLC", 12, 3_000, 55_000, "ring-doorbell"),
    (51, "10.20.0.51", "CC:BA:97:AA:00:51", "Espressif Inc.", None, 300, 500, ""),
    (52, "10.20.0.52", "CC:BA:97:AA:00:52", "Espressif Inc.", None, 200, 400, ""),
    (60, "hp laserjet", "10:5B:AD:AA:00:60", "Hewlett Packard", 1, 100, 100, "HP-LaserJet"),
    (70, "unifi-ap", "F4:92:BF:AA:00:70", "Ubiquiti Inc", 9, 2_000, 1_500, "unifi-ap"),
    (99, "10.20.0.99", "", "", None, 800, 600, ""),
]
FLOWS = [     # device octet, server ip, server name, port, l4, l7, share of the device's traffic, blacklisted
    (30, "198.51.100.10", "ipv4-c001.nflxvideo.net", 443, "TCP", "TLS.Netflix", 0.9, False),
    (40, "198.51.100.11", "ipv4-c002.nflxvideo.net", 443, "TCP", "TLS.Netflix", 0.8, False),
    (40, "198.51.100.30", "r3---sn.googlevideo.com", 443, "UDP", "QUIC.YouTube", 0.2, False),
    (20, "198.51.100.40", "github.com", 443, "TCP", "TLS.Github", 0.3, False),
    (20, "198.51.100.41", "objects.githubusercontent.com", 443, "TCP", "TLS.Github", 0.4, False),
    (20, "198.51.100.50", "api.anthropic.com", 443, "TCP", "TLS", 0.2, False),
    (21, "198.51.100.60", "teams.microsoft.com", 443, "UDP", "STUN.Teams", 0.7, False),
    (10, "198.51.100.70", "s3.backup.example.net", 443, "TCP", "TLS", 0.9, False),
    (11, "198.51.100.80", "198.51.100.80", 27018, "UDP", "Unknown", 0.6, False),
    (32, "203.0.113.66", "203.0.113.66", 4444, "TCP", "Unknown", 0.9, True),
    (32, "198.51.100.90", "graph.facebook.com", 443, "TCP", "TLS.Facebook", 0.05, False),
    (41, "198.51.100.95", "audio-ak.spotify.com", 443, "TCP", "TLS.Spotify", 0.9, False),
    (50, "198.51.100.96", "fw.ring.com", 443, "TCP", "TLS.AmazonAWS", 0.9, False),
    (51, "198.51.100.97", "a1.tuyaus.com", 8883, "TCP", "MQTT", 0.9, False),
    (52, "198.51.100.97", "a1.tuyaus.com", 8883, "TCP", "MQTT", 0.9, False),
    (31, "198.51.100.98", "gateway.icloud.com", 443, "TCP", "TLS.AppleiCloud", 0.8, False),
    (31, "10.99.0.1", "10.99.0.1", 443, "TCP", "TLS", 0.0, False),          # the DNS-block page
]
SINKHOLE = "10.99.0.1"
ROUTER = LAN + "1"
# The sample Wi-Fi: two access points, one cabled and one linked over Wi-Fi (mesh), and one of each kind of trouble.
AP_LIVING, AP_GARAGE = "f4:92:bf:aa:00:70", "f4:92:bf:aa:00:71"
WIFI_CLIENTS = [  # device octet, access point, radio, channel, signal dBm, share of frames re-sent, alias
    (30, AP_LIVING, "na", 36, -58, 0.03, "Sam's iPad"),
    (31, AP_LIVING, "na", 36, -66, 0.05, ""),
    (32, AP_GARAGE, "ng", 6, -69, 0.08, ""),
    (40, AP_LIVING, "na", 36, -52, 0.02, ""),
    (41, AP_LIVING, "ng", 1, -61, 0.09, ""),
    (50, AP_GARAGE, "ng", 6, -83, 0.34, "Ring doorbell"),
    (51, AP_LIVING, "ng", 1, -64, 0.07, ""),
    (52, AP_LIVING, "ng", 1, -67, 0.08, ""),
]


class SampleNetwork:
    def __init__(self):
        self.t0 = None
        self.last = None
        self.counters: dict[str, list[float]] = {}     # ip -> [sent, received], accumulated like real ones

    def snapshot(self, now: float) -> dict:
        self.t0 = self.t0 or now
        dt = max(0.0, now - (self.last or now))
        self.last = now
        wave = 1.0 + 0.25 * math.sin(now / 40.0)
        hosts, flows = [], []
        for octet, name, _mac, _vendor, _type, down, up, _dhcp in DEVICES:
            ip = LAN + str(octet)
            c = self.counters.setdefault(ip, [1e6, 1e6])
            c[0] += up * wave * dt
            c[1] += down * wave * dt
            hosts.append({"ip": ip, "name": name, "is_localhost": True, "is_broadcast": False, "is_multicast": False,
                          "is_blacklisted": False, "country": "", "first_seen": int(now - 7200), "last_seen": int(now),
                          "bytes": {"sent": int(c[0]), "recvd": int(c[1]), "total": 0},
                          "thpt": {"bps": (up + down) * 8 * wave, "pps": 10.0},
                          "num_flows": {"total": 6, "as_client": 5, "as_server": 1}, "num_alerts": 0,
                          "score": {"total": 9200 if octet == 32 else 40, "as_client": 0, "as_server": 0}})
        hosts.append({"ip": LAN + "255", "name": LAN + "255", "is_localhost": True, "is_broadcast": True, "is_multicast": False,
                      "bytes": {"sent": 0, "recvd": 100}, "thpt": {"bps": 0, "pps": 0}, "num_flows": {"total": 0, "as_client": 0, "as_server": 0},
                      "score": {"total": 0}, "first_seen": int(now), "last_seen": int(now)})
        rates = {d[0]: (d[5], d[6]) for d in DEVICES}
        for i, (octet, sip, sname, port, l4, l7, share, black) in enumerate(FLOWS):
            down, up = rates[octet]
            bps = (down + up) * share * wave
            for copy in range(70 if sip == SINKHOLE else 1):
                flows.append({"key": f"s{i}-{copy}", "hash_id": str(i), "first_seen": int(now - 600), "last_seen": int(now),
                              "bytes": int(bps * 600), "thpt": {"bps": bps * 8, "pps": 5.0}, "duration": 600, "vlan": 0,
                              "protocol": {"l4": l4, "l7": l7}, "breakdown": {"cli2srv": 30, "srv2cli": 70},
                              "client": {"ip": LAN + str(octet), "name": "", "port": 40000 + i, "is_blacklisted": False},
                              "server": {"ip": sip, "name": sname, "port": port, "is_blacklisted": black}})
        # The internet using the game server's port forward, and the Mac reading the NAS.
        flows.append({"key": "in1", "hash_id": "900", "first_seen": int(now - 300), "last_seen": int(now), "bytes": 4_000_000,
                      "thpt": {"bps": 320_000.0, "pps": 40.0}, "protocol": {"l4": "UDP", "l7": "Unknown"},
                      "client": {"ip": "192.0.2.44", "name": "192.0.2.44", "port": 51000, "is_blacklisted": False},
                      "server": {"ip": LAN + "11", "name": "", "port": 27015, "is_blacklisted": False}})
        # Someone on the VPN reading the NAS through the tunnel.
        flows.append({"key": "vpn1", "hash_id": "902", "first_seen": int(now - 600), "last_seen": int(now), "bytes": 30_000_000,
                      "thpt": {"bps": 800_000.0, "pps": 60.0}, "protocol": {"l4": "TCP", "l7": "SMBv23"},
                      "client": {"ip": VPN_TUNNEL, "name": "", "port": 53000, "is_blacklisted": False},
                      "server": {"ip": LAN + "10", "name": "", "port": 445, "is_blacklisted": False}})
        flows.append({"key": "lan1", "hash_id": "901", "first_seen": int(now - 900), "last_seen": int(now), "bytes": 90_000_000,
                      "thpt": {"bps": 1_600_000.0, "pps": 120.0}, "protocol": {"l4": "TCP", "l7": "SMBv23"},
                      "client": {"ip": LAN + "20", "name": "", "port": 52000, "is_blacklisted": False},
                      "server": {"ip": LAN + "10", "name": "", "port": 445, "is_blacklisted": False}})
        total = sum(d[5] + d[6] for d in DEVICES) * 8 * wave
        return {"interface": {"ifname": "sample0", "throughput_bps": total, "num_flows": len(flows), "num_hosts": len(hosts) + 14,
                              "num_local_hosts": len(hosts), "num_devices": len(DEVICES), "drops": 0}, "hosts": hosts, "flows": flows}

    def macs(self) -> dict[str, dict]:
        return {d[2]: {"vendor": d[3], "type": ""} for d in DEVICES if d[2]}

    def details(self) -> dict[str, dict]:
        return {LAN + str(d[0]): {"mac": d[2], "devtype": d[4] or 0} for d in DEVICES if d[2]}

    def leases(self) -> dict[str, dict]:
        return {LAN + str(d[0]): {"mac": d[2], "name": d[7], "ts": 0.0} for d in DEVICES if d[2] and d[7]}

    def vpn_status(self, now: float) -> dict:
        """What the router's sync script posts: the VPN server's own list of connected clients."""
        return {"servers": [{"name": "Home VPN UDP4:1194", "port": "1194", "mode": "server_tls_user", "conns": [
            {"common_name": "sam-phone", "user_name": "sam", "remote_host": "198.51.100.200:51820", "virtual_addr": VPN_TUNNEL,
             "bytes_recv": 41_000_000, "bytes_sent": 260_000_000, "connect_time_unix": int(now - 5400), "cipher": "AES-256-GCM"}]}]}

    def unifi(self, now: float) -> dict:
        """What the UniFi controller answers, in its own shapes: devices, connected clients, recent events."""
        self.t0 = self.t0 or now
        age = now - self.t0 + 7200
        by_octet = {d[0]: d for d in DEVICES}

        def ap(mac, name, ip, uplink, radios, sta):
            return {"mac": mac, "type": "uap", "model": "U6LR", "name": name, "ip": ip, "state": 1, "adopted": True, "version": "6.6.77",
                    "uptime": int(age), "num_sta": sta, "satisfaction": 92, "uplink": uplink, "last_seen": int(now),
                    "radio_table": [{"name": f"wifi{i}", "radio": r[0], "channel": r[1], "ht": r[2]} for i, r in enumerate(radios)],
                    "radio_table_stats": [{"name": f"wifi{i}", "radio": r[0], "channel": r[1], "cu_total": r[3], "cu_self_rx": r[4],
                                           "cu_self_tx": r[4], "num_sta": r[5], "satisfaction": 90, "tx_power": 20,
                                           "tx_packets": int(age * 400), "tx_retries": int(age * 30)} for i, r in enumerate(radios)]}
        devices = [
            ap(AP_LIVING, "Living room AP", LAN + "70", {"type": "wire", "speed": 1000, "full_duplex": True},
               [("ng", 1, 20, 78, 9, 3), ("na", 36, 80, 21, 8, 3)], 6),
            ap(AP_GARAGE, "Garage AP", LAN + "71", {"type": "wireless", "uplink_mac": AP_LIVING, "signal": -71},
               [("ng", 6, 20, 34, 12, 2), ("na", 149, 80, 12, 2, 0)], 2),
        ]
        clients = []
        for octet, ap_mac, radio, channel, signal, retry, alias in WIFI_CLIENTS:
            d = by_octet[octet]
            attempts = int(age * 60)
            clients.append({"mac": d[2].lower(), "hostname": d[7] or "", "name": alias, "ip": LAN + str(octet), "is_wired": False,
                            "ap_mac": ap_mac, "essid": "Home", "radio": radio, "radio_proto": "ax" if radio == "na" else "n",
                            "channel": channel, "signal": signal, "rssi": signal + 95, "noise": -96, "nss": 2,
                            "tx_rate": 6500 if signal < -80 else 72200 if radio == "ng" else 866700,
                            "rx_rate": 6500 if signal < -80 else 72200 if radio == "ng" else 780000,
                            "wifi_tx_attempts": attempts, "tx_retries": int(attempts * retry), "tx_packets": attempts,
                            "satisfaction": 38 if signal < -80 else 96, "uptime": 300 if octet == 50 else int(age),
                            "latest_assoc_time": int(now - 300) if octet == 50 else int(now - age), "idletime": 2,
                            "powersave_enabled": False, "oui": d[3], "tx_bytes-r": d[5], "rx_bytes-r": d[6]})
        bell = by_octet[50][2].lower()
        events = []
        for i, back in enumerate((3300, 2400, 1500, 300)):          # the doorbell dropping off and coming back
            events.append({"_id": f"sample-d{i}", "key": "EVT_WU_Disconnected", "time": int((self.t0 - back - 40) * 1000), "user": bell,
                           "ap": AP_GARAGE, "ssid": "Home", "hostname": "ring-doorbell", "duration": 800,
                           "msg": f"User[{bell}] disconnected from \"Home\" (13m 20s connected, 2.1M bytes, last AP[{AP_GARAGE}])"})
            events.append({"_id": f"sample-c{i}", "key": "EVT_WU_Connected", "time": int((self.t0 - back) * 1000), "user": bell,
                           "ap": AP_GARAGE, "ssid": "Home", "hostname": "ring-doorbell",
                           "msg": f"User[{bell}] has connected to AP[{AP_GARAGE}] with SSID \"Home\" on \"channel 6(ng)\""})
        events.append({"_id": "sample-r0", "key": "EVT_WU_Roam", "time": int((self.t0 - 5000) * 1000), "user": by_octet[31][2].lower(),
                       "ap_from": AP_GARAGE, "ap_to": AP_LIVING, "ssid": "Home",
                       "msg": f"User[{by_octet[31][2].lower()}] roams from AP[{AP_GARAGE}] to AP[{AP_LIVING}]"})
        return {"devices": devices, "clients": clients, "events": events, "health": [], "version": "sample"}

    def ping_ms(self, tid: str, ts: float) -> float | None:
        """A made-up ping result for one target at one moment (the same every time it is asked)."""
        r = (zlib.crc32(f"{tid}@{int(ts)}".encode()) % 1000) / 1000.0
        base = {"router": 0.4, "gateway": 3.1, "dns": 9.0}.get(tid)
        if tid.startswith("inet:"):
            base = 14.0
        elif tid == f"gear:{AP_LIVING.upper()}":
            base = 0.9
        elif tid == f"gear:{AP_GARAGE.upper()}":
            if r > 0.97:
                return None
            base = 6.5
        elif tid.endswith(":00:50"):                # the doorbell at the edge of the garage access point's reach
            if r > 0.62:
                return None
            base = 140.0
        elif base is None:
            base = 6.0 + (zlib.crc32(tid.encode()) % 20)
        return round(base * (0.8 + 0.6 * r), 2)

    def syslog_lines(self) -> list[str]:
        out = [
            "<29>openvpn[62851]: 198.51.100.200:51820 peer info: IV_PLAT=ios",
            "<29>openvpn[62851]: 198.51.100.200:51820 peer info: IV_GUI_VER=net.openvpn.connect.ios_3.4.2-5211",
            "<29>openvpn[3799]: user 'admin' could not authenticate.",
        ] + [f"<29>openvpn[62851]: TLS Error: cannot locate HMAC in incoming packet from [AF_INET]203.0.113.{90 + i % 3}:4{i:04d}"
             for i in range(9)]
        for i in range(140):
            src = f"203.0.113.{i % 23 + 1}"
            out.append(f"<134>filterlog[1]: 5,,,1770009021,wan0,match,block,in,4,0x0,,244,1,0,none,6,tcp,44,{src},192.0.2.1,40000,{(22, 23, 3389, 445, 8080)[i % 5]},0,S")
        for port in range(1000, 1024):
            out.append(f"<134>filterlog[1]: 5,,,1000000103,wan0,match,block,in,4,0x0,,244,1,0,none,6,tcp,44,203.0.113.200,192.0.2.1,40000,{port},0,S")
        return out
