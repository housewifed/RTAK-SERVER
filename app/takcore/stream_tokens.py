"""Short-lived, path-scoped HMAC tickets for WebRTC stream playback.

Stateless: a ticket is `base64url(path:exp).base64url(hmac)`. The web UI mints
one per playback for a logged-in session; MediaMTX forwards it as a query
param to the auth hook, which verifies it. Scoped to a single path and a few
seconds so a leaked ticket is near-useless.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def make_ticket(secret: bytes, path: str, ttl: float = 60.0) -> str:
    exp = int(time.time() + ttl)
    payload = f"{path}:{exp}".encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def verify_ticket(secret: bytes, path: str, token: str) -> bool:
    if not token or "." not in token:
        return False
    p_b64, _, s_b64 = token.partition(".")
    try:
        payload = _unb64(p_b64)
        sig = _unb64(s_b64)
    except Exception:  # noqa: BLE001
        return False
    expected = hmac.new(secret, payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        tok_path, exp = payload.decode().rsplit(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    if tok_path != path:
        return False
    try:
        return time.time() <= int(exp)
    except ValueError:
        return False
