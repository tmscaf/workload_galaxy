"""Small shared helpers."""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def traffic_level(bytes_per_sec: float) -> float:
    """0..1 on a log scale: 1 kB/s is faint, 1 MB/s and up is full brightness."""
    if not bytes_per_sec or bytes_per_sec <= 0:
        return 0.0
    return max(0.0, min(1.0, (math.log10(bytes_per_sec) - 2.0) / 4.0))


def rate(bytes_per_sec: float | None) -> str:
    """Bytes per second as people read network speed: bits per second."""
    b = (bytes_per_sec or 0) * 8
    for unit, size in (("Gbps", 1e9), ("Mbps", 1e6), ("kbps", 1e3)):
        if b >= size:
            return f"{b / size:.1f} {unit}" if b / size < 100 else f"{b / size:.0f} {unit}"
    return f"{b:.0f} bps"


def size(n: float | None) -> str:
    n = float(n or 0)
    for unit, s in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= s:
            return f"{n / s:.1f} {unit}" if n / s < 100 else f"{n / s:.0f} {unit}"
    return f"{n:.0f} B"


def ago(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    s = int(max(0, seconds))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60} min"
    if s < 172800:
        return f"{s // 3600} h"
    return f"{s // 86400} days"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "x"


def plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


def now() -> float:
    return time.time()
