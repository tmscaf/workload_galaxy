"""Small helpers shared by the collector modules."""
from __future__ import annotations

import ipaddress
import re
from datetime import datetime, timezone
from typing import Any

_CPU_SUFFIX = {"n": 1e-9, "u": 1e-6, "m": 1e-3}
_MEM_SUFFIX = {
    "Ki": 1024, "Mi": 1024 ** 2, "Gi": 1024 ** 3, "Ti": 1024 ** 4, "Pi": 1024 ** 5, "Ei": 1024 ** 6,
    "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "m": 1e-3,
}
_QTY = re.compile(r"^([+-]?[0-9.]+(?:[eE][+-]?[0-9]+)?)([A-Za-z]*)$")


def meta(obj: dict | None) -> dict:
    return (obj or {}).get("metadata") or {}


def name_of(obj: dict | None) -> str:
    return meta(obj).get("name", "")


def ns_of(obj: dict | None) -> str:
    return meta(obj).get("namespace", "")


def labels_of(obj: dict | None) -> dict:
    return meta(obj).get("labels") or {}


def controller_ref(obj: dict | None) -> dict | None:
    refs = meta(obj).get("ownerReferences") or []
    for ref in refs:
        if ref.get("controller"):
            return ref
    return refs[0] if refs else None


def parse_cpu(q: Any) -> float | None:
    """Kubernetes CPU quantity -> cores."""
    if q is None:
        return None
    m = _QTY.match(str(q).strip())
    if not m:
        return None
    value, suffix = float(m.group(1)), m.group(2)
    if suffix in _CPU_SUFFIX:
        return value * _CPU_SUFFIX[suffix]
    if suffix == "":
        return value
    return None


def parse_mem(q: Any) -> float | None:
    """Kubernetes memory quantity -> bytes."""
    if q is None:
        return None
    m = _QTY.match(str(q).strip())
    if not m:
        return None
    value, suffix = float(m.group(1)), m.group(2)
    if suffix == "":
        return value
    mult = _MEM_SUFFIX.get(suffix)
    return value * mult if mult else None


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds").replace("+00:00", "Z")


def is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def normalize_ip(value: str) -> str:
    """Strip IPv4-mapped IPv6 prefixes, brackets and zone ids."""
    value = value.strip().strip("[]")
    if "%" in value:
        value = value.split("%", 1)[0]
    if value.lower().startswith("::ffff:") and "." in value:
        value = value[7:]
    return value


def ip_is_local(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def noisy_or(weights: list[float]) -> float:
    """Combine independent risk signals: 1 - prod(1 - w)."""
    p = 1.0
    for w in weights:
        p *= 1.0 - max(0.0, min(1.0, w))
    return 1.0 - p


def level_for(score: float) -> str:
    if score >= 0.6:
        return "critical"
    if score >= 0.25:
        return "warning"
    return "ok"
