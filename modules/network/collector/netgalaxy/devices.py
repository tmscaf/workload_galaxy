# Copyright (C) 2026 Troy Scafuro
# SPDX-License-Identifier: AGPL-3.0-only

"""The device inventory: who is on the network, what each one is, and what is normal for it.

A device is keyed by its MAC address when one is known (addresses move between devices on a
network without fixed leases) and by its IP address until then. The inventory outlives restarts
in a small JSON file, which is what makes "new device" and "unusual for this device" possible.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time

log = logging.getLogger(__name__)

# Groups the map is laid out in (the front end's "projects"), in display order.
GROUPS = [
    ("servers", "Servers"), ("computers", "Computers"), ("phones-tablets", "Phones & tablets"),
    ("tv-media", "TV & media"), ("smart-home", "Smart home"), ("printers", "Printers"),
    ("network-gear", "Network gear"), ("vpn", "VPN connections"), ("unidentified", "Unidentified"),
]
GROUP_TITLES = dict(GROUPS)
TYPE_LABEL = {
    "server": "Server", "vm": "Virtual machine", "computer": "Computer", "laptop": "Laptop", "phone": "Phone",
    "tablet": "Tablet", "tv": "TV", "media": "Media player", "console": "Game console", "iot": "Smart device",
    "camera": "Camera", "printer": "Printer", "nas": "Storage", "network": "Network device", "wifi": "Wi-Fi access point",
    "unknown": "Unknown device",
}
TYPE_GROUP = {
    "server": "servers", "vm": "servers", "nas": "servers", "computer": "computers", "laptop": "computers",
    "phone": "phones-tablets", "tablet": "phones-tablets", "tv": "tv-media", "media": "tv-media", "console": "tv-media",
    "iot": "smart-home", "camera": "smart-home", "printer": "printers", "network": "network-gear", "wifi": "network-gear",
    "unknown": "unidentified",
}
# ntopng's device type numbers
NTOP_TYPES = {1: "printer", 2: "camera", 3: "computer", 4: "laptop", 5: "tablet", 6: "phone", 7: "tv", 8: "network",
              9: "wifi", 10: "nas", 11: "media", 12: "iot"}
VM_OUIS = ("00:0C:29", "00:50:56", "00:05:69", "00:1C:14", "52:54:00", "00:15:5D", "08:00:27", "BC:24:11")
NAME_HINTS = [      # (pattern on the name, type) - first match wins
    (r"ipad|galaxy tab|kindle|fire hd", "tablet"), (r"iphone|android|pixel|galaxy|oneplus|phone", "phone"),
    (r"\btv\b|roku|firetv|fire-tv|chromecast|bravia|samsung q|lg ?web|shield|apple-?tv|vizio|tizen", "tv"),
    (r"spotifyconnect|sonos|echo|homepod|google-home|nest-audio|denon|yamaha|bose|heos|^amazon-", "media"),
    (r"^npi[0-9a-f]{6}|^hp[0-9a-f]{6}|^brw[0-9a-f]{6}|^epson[0-9a-f]{6}", "printer"),
    (r"tesla|wallconnector|powerwall|enphase|solaredge|chargepoint", "iot"),
    (r"threadripper|ryzen|strix|\brog\b|gaming|alienware|predator|tower", "computer"),
    (r"playstation|ps4|ps5|xbox|nintendo|switch-|steamdeck", "console"),
    (r"ring-|ring\b|nest|hue|wyze|esp[-_]|tasmota|shelly|tuya|smartthings|ecobee|myq|kasa|wemo|roomba|govee", "iot"),
    (r"cam\b|camera|doorbell|arlo|blink", "camera"),
    (r"laserjet|officejet|deskjet|printer|epson|brother|canon", "printer"),
    (r"unifi|ubiquiti|\bap\b|switch|router|pfsense|gateway|orbi|eero|mesh", "network"),
    (r"nas|synology|qnap|truenas", "nas"), (r"macbook|laptop|thinkpad|xps|surface", "laptop"),
    (r"\bmac\b|imac|mac-?mini|desktop|\bpc\b|workstation|win-|linux|ubuntu|debian", "computer"),
    (r"server|esxi|proxmox|k3s|docker|\bvm\b", "server"),
]
VENDOR_HINTS = [
    (r"ubiquiti|tp-link|netgear|aruba|cisco|mikrotik|eero|netgate|adi engineering", "network"),
    (r"espressif|tuya|ring|shenzhen|wyze|ecobee|signify|philips lighting|sonoff|chamberlain|irobot", "iot"),
    (r"sonos|roku|bose|denon", "media"), (r"nintendo|sony interactive|microsoft.*xbox", "console"),
    (r"samsung|lg electronics|vizio|tcl|hisense", "tv"),
    (r"hewlett|hp inc|canon|epson|brother", "printer"), (r"synology|qnap", "nas"),
    (r"vmware|proxmox|qemu|xensource|microsoft corp", "vm"), (r"dell|lenovo|intel|asustek|gigabyte|micro-star|supermicro", "computer"),
]
_UUIDISH = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4,}|^[0-9a-f]{12,}$", re.I)
_IPISH = re.compile(r"^\d+\.\d+\.\d+\.\d+$|:")


def is_random_mac(mac: str) -> bool:
    """Locally administered = a private address a phone or tablet made up for this network."""
    return len(mac) >= 2 and mac[1].upper() in "26AE"


def clean_name(name: str | None, ip: str = "") -> str:
    """A name worth showing, or '' when the 'name' is really an address or a random identifier."""
    n = (name or "").strip().strip(".")
    if not n or n == ip or _IPISH.match(n) or _UUIDISH.search(n) or n.lower() in ("noip", "localhost", "unknown"):
        return ""
    for suffix in (".local", ".lan", ".home.arpa", ".localdomain", ".home"):
        if n.lower().endswith(suffix):
            n = n[: -len(suffix)]
    return n


def classify(name: str, vendor: str, mac: str, ntop_type: int | None, model: str = "") -> str:
    low = name.lower()
    for pattern, kind in NAME_HINTS:
        if low and re.search(pattern, low):
            return kind
    if mac and mac.upper().startswith(VM_OUIS):
        return "vm"         # outranks ntopng's generic "workstation"
    if ntop_type in NTOP_TYPES:
        return NTOP_TYPES[ntop_type]
    v = vendor.lower()
    for pattern, kind in VENDOR_HINTS:
        if v and re.search(pattern, v):
            return kind
    if "apple" in v or re.match(r"mac(book)?\d", (model or "").lower()):
        return "computer"
    if re.match(r"ipad|j\d{3}[a-z]*ap$", (model or "").lower()):
        return "tablet"
    if re.match(r"iphone|d\d{2,3}[a-z]*ap$", (model or "").lower()):
        return "phone"
    if mac and is_random_mac(mac):
        return "phone"
    return "unknown"


class Inventory:
    """Devices by key, with a slow-moving picture of each one's normal traffic."""

    ALPHA = 0.05            # EWMA weight of one sample (30 s refresh -> ~10 min memory per step, hours overall)
    SAVE_EVERY = 180.0

    def __init__(self, data_dir: str = "", overrides: dict | None = None):
        self.path = os.path.join(data_dir, "devices.json") if data_dir else ""
        self.devices: dict[str, dict] = {}
        self.by_ip: dict[str, str] = {}
        self.created_at = time.time()
        self.overrides = self._index_overrides(overrides or {})
        self._saved_at = 0.0
        self._lock = threading.Lock()
        self._load()

    @staticmethod
    def _index_overrides(raw: dict) -> dict:
        """{"devices": [{"mac"|"ip": ..., "name": ..., "type": ..., "group": ...}]} -> lookup by MAC / IP."""
        out = {}
        for d in raw if isinstance(raw, list) else []:
            for k in ("mac", "ip"):
                if d.get(k):
                    out[str(d[k]).upper() if k == "mac" else str(d[k])] = d
        return out

    def _load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.devices = data.get("devices", {})
            self.created_at = data.get("createdAt", self.created_at)
            for key, d in self.devices.items():
                if d.get("ip"):
                    self.by_ip[d["ip"]] = key
            log.info("inventory: %d devices known from %s", len(self.devices), self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as err:
            log.warning("inventory file unreadable (%s); starting empty", err)

    def save(self, force: bool = False) -> None:
        if not self.path or (not force and time.time() - self._saved_at < self.SAVE_EVERY):
            return
        with self._lock:
            payload = json.dumps({"createdAt": self.created_at, "devices": self.devices})
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
            self._saved_at = time.time()
        except OSError as err:
            log.warning("cannot save inventory: %s", err)

    @property
    def learning(self) -> bool:
        """During the first day everything is 'new'; nothing should be reported as such."""
        return time.time() - self.created_at < 86400

    # ------------------------------------------------------------------ updates
    def observe(self, ip: str, *, now: float, name: str = "", mac: str = "", vendor: str = "", ntop_type: int | None = None,
                dhcp_name: str = "", up_bps: float = 0.0, down_bps: float = 0.0, peers: int = 0, active: bool = True,
                model: str = "", alias: str = "") -> dict:
        """Fold one sighting of an address into the inventory and return its device record."""
        mac = (mac or "").upper()
        with self._lock:
            key = self.by_ip.get(ip)
            if mac:
                if key and key != mac and not self.devices.get(key, {}).get("mac"):
                    # We knew this address before we knew its MAC: the same device, re-keyed.
                    self.devices[mac] = {**self.devices.pop(key), **self.devices.get(mac, {})}
                elif key and key != mac:
                    pass        # the address moved to another device; the old record keeps its history
                key = mac
            key = key or ip
            d = self.devices.setdefault(key, {"firstSeen": now, "samples": 0, "upAvg": 0.0, "downAvg": 0.0, "peersAvg": 0.0})
            self.by_ip[ip] = key
            d["ip"] = ip
            if mac:
                d["mac"] = mac
            if vendor:
                d["vendor"] = vendor
            if model:
                d["model"] = model
            if ntop_type:
                d["ntopType"] = ntop_type
            if clean_name(dhcp_name, ip):
                d["dhcpName"] = clean_name(dhcp_name, ip)
            if alias and clean_name(alias, ip):
                d["alias"] = clean_name(alias, ip)          # a name someone typed into the Wi-Fi controller
            if clean_name(name, ip):
                d["seenName"] = clean_name(name, ip)
            if active:
                d["lastSeen"] = now
                # What is normal: a slow average of each rate, learned only from samples that are not
                # themselves far out of line (so one burst does not become the new normal at once).
                a = self.ALPHA
                for field, value in (("upAvg", up_bps), ("downAvg", down_bps), ("peersAvg", float(peers))):
                    base = d.get(field, 0.0)
                    capped = min(value, max(base * 4, value if d["samples"] < 20 else base * 4))
                    d[field] = base + a * (capped - base)
                d["samples"] = d.get("samples", 0) + 1
            return d

    def describe(self, key: str) -> dict:
        """Name, type and group for a device, applying overrides and the heuristics."""
        d = self.devices[key]
        mac, ip = d.get("mac", ""), d.get("ip", "")
        o = self.overrides.get(mac) or self.overrides.get(ip) or {}
        dhcp, seen = d.get("dhcpName", ""), d.get("seenName", "")
        # The name a device announces on the network ("HP Color LaserJet M281") beats a generated
        # lease name ("NPI0A1B2C"); a lease name beats a bare word.
        name = o.get("name") or d.get("alias") or (seen if " " in seen else "") or dhcp or seen or ""
        vendor = d.get("vendor", "")
        kind = o.get("type") or classify(f"{name} {dhcp} {seen}".strip(), vendor, mac, d.get("ntopType"), d.get("model", ""))
        if kind not in TYPE_LABEL:
            kind = "unknown"
        group = o.get("group") if o.get("group") in GROUP_TITLES else TYPE_GROUP[kind]
        if not name:
            tail = ip.rsplit(".", 1)[-1] if "." in ip else ip[-4:]
            private = bool(mac) and is_random_mac(mac)
            what = vendor.split(",")[0].split(" Inc")[0].strip() if vendor else ("Private-address device" if private else TYPE_LABEL[kind])
            name = f"{what} ·{tail}"
            named = False
        else:
            named = True
        return {"key": key, "name": name, "named": named, "type": kind, "typeLabel": TYPE_LABEL[kind], "group": group,
                "vendor": vendor, "model": d.get("model", ""), "mac": mac, "ip": ip,
                "randomMac": bool(mac) and is_random_mac(mac),
                "pinned": bool(o)}

    def items(self) -> list[tuple[str, dict]]:
        with self._lock:
            return list(self.devices.items())

    def prune(self, now: float, keep_seconds: float = 30 * 86400) -> None:
        with self._lock:
            for key in [k for k, d in self.devices.items() if now - d.get("lastSeen", d.get("firstSeen", now)) > keep_seconds]:
                d = self.devices.pop(key)
                if self.by_ip.get(d.get("ip")) == key:
                    self.by_ip.pop(d.get("ip"), None)
