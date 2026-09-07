"""Web UI authentication: password hashing, sessions, and role checks.

Stdlib-only. Sessions are in-memory signed tokens (fine for a single-node
on-prem server; they reset on restart, which just means re-login).

Roles (increasing privilege): viewer < operator < admin
  viewer   - read the map, video, chat, history
  operator - + send chat, add/remove cameras
  admin    - + enroll devices, manage users
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Dict, Optional

ROLES = ("viewer", "operator", "admin")
ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

SESSION_TTL = 12 * 3600  # 12 hours


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """PBKDF2-HMAC-SHA256; returns 'salt$hexhash'."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt),
                             120_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


class SessionManager:
    def __init__(self) -> None:
        self._sessions: Dict[str, dict] = {}

    def create(self, username: str, role: str) -> str:
        token = secrets.token_urlsafe(32)
        self._sessions[token] = {
            "username": username, "role": role,
            "expires": time.time() + SESSION_TTL,
        }
        return token

    def get(self, token: Optional[str]) -> Optional[dict]:
        if not token:
            return None
        s = self._sessions.get(token)
        if s is None:
            return None
        if time.time() > s["expires"]:
            self._sessions.pop(token, None)
            return None
        return s

    def destroy(self, token: Optional[str]) -> None:
        if token:
            self._sessions.pop(token, None)


class LoginThrottle:
    """In-memory failed-login throttle keyed on (ip, username).

    After `max_fails` failures within `window` seconds, the key is locked for
    `lockout` seconds; further failures while locked re-arm the lockout. A
    successful login clears the key. Single-node, in-memory (resets on
    restart), which is appropriate for this server.
    """

    def __init__(self, max_fails: int = 5, window: float = 900.0,
                 lockout: float = 60.0, now_fn=time.time) -> None:
        self.max_fails = max_fails
        self.window = window
        self.lockout = lockout
        self._now = now_fn
        self._fails: Dict[object, list] = {}
        self._locked_until: Dict[object, float] = {}
        self._lock = threading.Lock()

    def check(self, key) -> Optional[float]:
        now = self._now()
        with self._lock:
            until = self._locked_until.get(key, 0.0)
            return until - now if until > now else None

    def _evict_stale(self, now: float) -> None:
        for k in list(self._fails):
            recent = [t for t in self._fails[k] if now - t < self.window]
            if not recent and self._locked_until.get(k, 0.0) <= now:
                self._fails.pop(k, None)
                self._locked_until.pop(k, None)
            else:
                self._fails[k] = recent

    def record_failure(self, key) -> None:
        now = self._now()
        with self._lock:
            self._evict_stale(now)
            fails = [t for t in self._fails.get(key, []) if now - t < self.window]
            fails.append(now)
            self._fails[key] = fails
            if len(fails) >= self.max_fails:
                self._locked_until[key] = now + self.lockout

    def record_success(self, key) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._locked_until.pop(key, None)


def role_allows(role: str, required: str) -> bool:
    return ROLE_RANK.get(role, -1) >= ROLE_RANK.get(required, 99)


# Endpoints that must never require a login: device/MediaMTX callbacks and the
# enrollment artifacts phones download. Everything else under /api requires a
# valid session when auth is enabled.
PUBLIC_API_PATHS = frozenset({
    "/api/login", "/api/logout", "/api/me",
    "/api/mediamtx/auth",
    "/truststore.p12", "/api/truststore.p12", "/enroll.zip",
    "/ca.mobileconfig",
})


def default_admin() -> tuple:
    """Seed credentials from env, or ('admin','admin') with a warning."""
    user = os.environ.get("ADMIN_USER", "admin")
    pw = os.environ.get("ADMIN_PASSWORD", "")
    return user, pw


def must_refuse_start(auth_enabled: bool, admin_password: str,
                      user_count: int) -> bool:
    """True when the server must refuse to boot: auth is on, there are no
    users yet, and no ADMIN_PASSWORD was provided. Prevents silently seeding
    a well-known admin/admin credential."""
    return bool(auth_enabled) and user_count == 0 and not admin_password
