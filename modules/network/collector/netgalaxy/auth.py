"""A password for actions. Viewing stays open on the LAN; blocking and unblocking need a session.

The session is a signed cookie (HMAC over expiry), HttpOnly and SameSite=Strict, and every action
must also carry the X-Requested-With header, so another web page cannot submit one on your behalf.
Repeated wrong passwords from one address are locked out for a while.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

COOKIE = "ng_session"
SESSION_SECONDS = 12 * 3600
MAX_FAILURES = 5
LOCKOUT_SECONDS = 300
ACTION_HEADER = "x-requested-with"


class Auth:
    def __init__(self, password: str):
        self.password = password or ""
        self.key = secrets.token_bytes(32)        # sessions end when the app restarts
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def locked(self, client: str, now: float | None = None) -> float:
        """Seconds until this client may try again (0 = may try now)."""
        now = now or time.time()
        with self._lock:
            recent = [t for t in self._fails.get(client, []) if now - t < LOCKOUT_SECONDS]
            self._fails[client] = recent
            return round(LOCKOUT_SECONDS - (now - recent[0])) if len(recent) >= MAX_FAILURES else 0

    def login(self, client: str, password: str, now: float | None = None) -> str | None:
        now = now or time.time()
        if not self.enabled or self.locked(client, now):
            return None
        if hmac.compare_digest(hashlib.sha256(password.encode()).digest(), hashlib.sha256(self.password.encode()).digest()):
            with self._lock:
                self._fails.pop(client, None)
            return self._token(now + SESSION_SECONDS)
        with self._lock:
            self._fails.setdefault(client, []).append(now)
        return None

    def _token(self, expires: float) -> str:
        exp = str(int(expires))
        return exp + "." + hmac.new(self.key, exp.encode(), hashlib.sha256).hexdigest()

    def valid(self, token: str | None, now: float | None = None) -> bool:
        if not self.enabled or not token or "." not in token:
            return False
        exp, sig = token.split(".", 1)
        if not exp.isdigit() or int(exp) < (now or time.time()):
            return False
        return hmac.compare_digest(sig, hmac.new(self.key, exp.encode(), hashlib.sha256).hexdigest())
