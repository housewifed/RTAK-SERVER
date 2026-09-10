"""HTTP server for the web UI: static files, REST API, SSE live feed.

Runs stdlib ``ThreadingHTTPServer`` in a background thread; each SSE
connection holds one thread, which is fine for the handful of concurrent
browser sessions an ops map has. The TAK side stays on the asyncio loop and
communicates with web subscribers through thread-safe queues owned by the
Hub.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from http.cookies import SimpleCookie

from . import auth as authmod
from .auth import SessionManager
from .enrollment import EnrollmentService
from .hub import Hub
from .mediamtx import StreamRegistry
from .store import Store

log = logging.getLogger("takcore.web")

# Minimum role for write endpoints (GET defaults to viewer = any logged-in user)
ENDPOINT_ROLES = {
    ("POST", "/api/enroll/tokens"): "admin",
    ("POST", "/api/enroll/bulk"): "admin",
    ("DELETE", "/api/enroll/tokens"): "admin",
    ("GET", "/api/users"): "admin",
    ("POST", "/api/users"): "admin",
    ("POST", "/api/users/role"): "admin",
    ("DELETE", "/api/users"): "admin",
    ("DELETE", "/api/devices"): "admin",
    ("DELETE", "/api/alerts"): "operator",
    ("DELETE", "/api/chat"): "admin",
    ("POST", "/api/tracks"): "operator",
    ("POST", "/api/tracks/stop"): "operator",
    ("DELETE", "/api/tracks"): "operator",
    ("POST", "/api/chat"): "operator",
    ("POST", "/api/streams"): "operator",
    ("POST", "/api/streams/record"): "operator",
    ("DELETE", "/api/streams"): "operator",
}


def _float_arg(query, name: str) -> Optional[float]:
    """One optional float from a parsed query string, ignoring junk."""
    raw = (query.get(name) or [""])[0]
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def https_enabled(env) -> bool:
    """True when Caddy is fronting the server with a real certificate."""
    domain = env.get("TAK_DOMAIN", "")
    return bool(domain) and domain != "localhost"


def public_web_base(env) -> str:
    """The base URL a PHONE must use to reach the web server.

    Not the address the admin's browser happens to be on: with HTTPS enabled
    the enrollment package and the iOS profile come through Caddy on 443, while
    takcore's own port is usually not reachable from outside at all. Returns ""
    when SERVER_HOST is unset and there is no domain, so callers can fall back.
    """
    if https_enabled(env):
        port = env.get("CADDY_HTTPS_PORT") or "443"
        suffix = "" if str(port) == "443" else f":{port}"
        return f"https://{env.get('TAK_DOMAIN', '')}{suffix}"
    host = env.get("SERVER_HOST", "")
    if not host:
        return ""
    return f"http://{host}:{env.get('HTTP_PORT') or '8080'}"


def make_handler(hub: Hub, store: Store, web_dir: str,
                 registry: Optional[StreamRegistry] = None,
                 enroll: Optional[EnrollmentService] = None,
                 sessions: Optional[SessionManager] = None,
                 auth_enabled: bool = False,
                 throttle: Optional["authmod.LoginThrottle"] = None):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "takcore/0.1"

        # -- helpers ------------------------------------------------------

        def _binary(self, data: bytes, ctype: str, filename: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def _json(self, obj, status: int = 200) -> None:
            body = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # API data is live (stream readiness, device state); never let the
            # browser serve a cached copy or the map goes stale until a manual
            # reload.
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:  # quiet access log
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _body_json(self) -> Optional[dict]:
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length <= 0 or length > 1_000_000:
                    return None
                return json.loads(self.rfile.read(length))
            except (ValueError, json.JSONDecodeError):
                return None

        def _session(self) -> Optional[dict]:
            if sessions is None:
                return None
            raw = self.headers.get("Cookie", "")
            if not raw:
                return None
            try:
                ck = SimpleCookie(raw)
            except Exception:  # noqa: BLE001
                return None
            tok = ck["takcore_session"].value if "takcore_session" in ck else None
            return sessions.get(tok)

        def _authorized(self, method: str, path: str) -> bool:
            """Returns True if the request may proceed. Sends 401/403 and
            returns False otherwise. Only gates /api/* (minus public paths)."""
            if not auth_enabled:
                return True
            if not path.startswith("/api/"):
                return True  # static shell is public; data is what's protected
            if path in authmod.PUBLIC_API_PATHS:
                return True
            sess = self._session()
            if sess is None:
                self._json({"error": "login required"}, 401)
                return False
            required = ENDPOINT_ROLES.get((method, path), "viewer")
            if not authmod.role_allows(sess["role"], required):
                self._json({"error": f"requires {required} role"}, 403)
                return False
            return True

        # -- routes -------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            path = urlparse(self.path).path
            if not self._authorized("GET", path):
                return
            try:
                if path == "/api/me":
                    sess = self._session()
                    if sess:
                        self._json({"username": sess["username"],
                                    "role": sess["role"], "auth": True})
                    else:
                        self._json({"auth": auth_enabled and False,
                                    "auth_required": auth_enabled}, 200)
                elif path == "/api/users":
                    self._json(store.list_users())  # admin-only (no pw hashes)
                elif path == "/api/devices":
                    self._json(store.devices())
                elif path == "/api/streams":
                    self._json(registry.list_streams() if registry else [])
                elif path.startswith("/api/devices/") and path.endswith("/track"):
                    uid = path[len("/api/devices/"):-len("/track")]
                    self._json(store.track(uid))
                elif path.startswith("/api/devices/") and path.endswith("/streams"):
                    uid = path[len("/api/devices/"):-len("/streams")]
                    self._json(registry.streams_for_device(uid) if registry else [])
                elif path.startswith("/api/devices/"):
                    uid = path[len("/api/devices/"):]
                    dev = store.device(uid)
                    self._json(dev if dev else {"error": "not found"},
                               200 if dev else 404)
                elif path == "/api/streams/ticket":
                    q = parse_qs(urlparse(self.path).query)
                    spath = (q.get("path") or [""])[0]
                    if registry is None or not spath:
                        self._json({"error": "path required"}, 400)
                    else:
                        self._json(registry.make_ticket(spath))
                elif path == "/api/stats":
                    self._json(hub.stats())
                elif path == "/api/chat":
                    self._json(store.chat_history())
                elif path == "/api/alerts":
                    self._json(store.active_alerts())
                elif path == "/api/recordings":
                    q = parse_qs(urlparse(self.path).query)
                    spath = (q.get("path") or [""])[0]
                    if not spath or registry is None:
                        self._json([])
                    else:
                        pb = os.environ.get("MEDIAMTX_PLAYBACK",
                                            "http://mediamtx:9996")
                        self._json(registry.mtx.recordings(spath, pb))
                elif path == "/api/tracks":
                    self._json(store.track_sessions())
                elif path == "/api/history":
                    q = parse_qs(urlparse(self.path).query)
                    # from/to/uids replay a recorded session or a custom range;
                    # minutes= is the original "last N minutes" form.
                    since = _float_arg(q, "from")
                    until = _float_arg(q, "to")
                    uids = [u for u in (q.get("uids") or [""])[0].split(",") if u]
                    mins = _float_arg(q, "minutes") or 60.0
                    self._json(store.history(minutes=min(mins, 1440),
                                             since=since, until=until,
                                             uids=uids or None))
                elif path == "/api/config":
                    # web_base is the address a PHONE must use to fetch the
                    # enrollment package and the iOS profile. It is not the
                    # address this browser is on: with HTTPS enabled those come
                    # through Caddy on 443, not through takcore's own port, and
                    # an admin may well be on the LAN IP while the phone is not.
                    self._json({"server_host": os.environ.get("SERVER_HOST",
                                                              ""),
                                "publish_token": os.environ.get("PUBLISH_TOKEN",
                                                                ""),
                                "web_base": public_web_base(os.environ),
                                "https": https_enabled(os.environ),
                                "http_port": os.environ.get("HTTP_PORT",
                                                            "8080"),
                                "tls_port": os.environ.get("TAK_TLS_PORT",
                                                           "8089"),
                                "enroll_port": os.environ.get("ENROLL_PORT",
                                                              "8446")})
                elif path == "/ca.mobileconfig":
                    log.info("ca.mobileconfig downloaded by %s",
                             self.address_string())
                    data = enroll.ios_trust_profile() if enroll else None
                    if data is None:
                        self._json({"error": "no CA"}, 404)
                    else:
                        self._binary(data, "application/x-apple-aspen-config",
                                     "ca.mobileconfig")
                elif path in ("/truststore.p12", "/api/truststore.p12"):
                    log.info("truststore.p12 downloaded by %s",
                             self.address_string())
                    data = enroll.truststore_bytes() if enroll else None
                    if data is None:
                        self._json({"error": "no truststore"}, 404)
                    else:
                        self._binary(data, "application/x-pkcs12",
                                     "truststore.p12")
                elif path == "/enroll.zip":
                    q = parse_qs(urlparse(self.path).query)
                    host = (q.get("host") or [os.environ.get("SERVER_HOST", "")])[0]
                    callsign = (q.get("callsign") or [None])[0]
                    # mode=softcert (default): zero-typing, server signs a
                    # client cert. mode=enroll: token-prompt auto-enrollment.
                    mode = (q.get("mode") or ["softcert"])[0]
                    if not (enroll and host):
                        self._json({"error": "enrollment/host unavailable"}, 404)
                        return
                    if mode == "enroll":
                        # token-prompt package: carries only CA trust + server
                        # config, no client identity, so the device still has to
                        # present the token at signClient. Safe to serve openly.
                        try:
                            pkg = enroll.data_package(host, callsign)
                        except Exception:  # noqa: BLE001
                            log.exception("data package build failed")
                            pkg = None
                    else:
                        # softcert: this bundle CONTAINS a ready-to-use mTLS
                        # client identity. Serving it without a token would hand
                        # any anonymous caller a working device certificate and
                        # defeat client-cert auth entirely, so require a valid
                        # enrollment token. We do NOT burn the token here: ATAK
                        # fetches enroll.zip more than once while importing (and
                        # users retry), so single-use made the QR work exactly
                        # once and then 403 on every retry. The token still gates
                        # anonymous access and expires on its own (default 24h).
                        username = (q.get("username") or [""])[0]
                        token = (q.get("token") or [""])[0]
                        if not (username and token
                                and enroll.verify(username, token)):
                            log.warning("enroll.zip softcert denied: bad/missing "
                                        "token for %r from %s", username,
                                        self.address_string())
                            self._json({"error": "valid enrollment token "
                                        "required"}, 403)
                            return
                        try:
                            pkg = enroll.softcert_package(host, callsign)
                        except Exception:  # noqa: BLE001
                            log.exception("softcert package build failed")
                            pkg = None
                    if pkg is None:
                        self._json({"error": "package build failed"}, 500)
                    else:
                        log.info("enroll.zip (mode=%s, %d bytes) downloaded "
                                 "by %s", mode, len(pkg), self.address_string())
                        self._binary(pkg, "application/zip",
                                     "TAK-Revamp_Connect.zip")
                elif path == "/api/enroll/tokens":
                    self._json(store.enroll_users() if enroll else [])
                elif path == "/api/stream":
                    self._sse()
                else:
                    self._static(path)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001
                log.exception("error handling %s", path)
                try:
                    self._json({"error": "internal"}, 500)
                except Exception:  # noqa: BLE001
                    pass

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if not self._authorized("POST", path):
                return
            try:
                if path == "/api/login":
                    body = self._body_json() or {}
                    username = (body.get("username") or "").strip()
                    key = (self.client_address[0], username)
                    if throttle is not None:
                        remaining = throttle.check(key)
                        if remaining is not None:
                            self.send_response(429)
                            self.send_header("Retry-After",
                                             str(int(remaining) + 1))
                            self.send_header("Content-Length", "0")
                            self.end_headers()
                            return
                    user = store.get_user(username)
                    if (user and sessions and
                            authmod.verify_password(body.get("password") or "",
                                                    user["pw_hash"])):
                        if throttle is not None:
                            throttle.record_success(key)
                        tok = sessions.create(user["username"], user["role"])
                        ck = SimpleCookie()
                        ck["takcore_session"] = tok
                        ck["takcore_session"]["path"] = "/"
                        ck["takcore_session"]["httponly"] = True
                        ck["takcore_session"]["samesite"] = "Lax"
                        body_b = json.dumps({"username": user["username"],
                                             "role": user["role"]}).encode()
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body_b)))
                        self.send_header("Set-Cookie",
                                         ck["takcore_session"].OutputString())
                        self.end_headers()
                        self.wfile.write(body_b)
                    else:
                        if throttle is not None:
                            throttle.record_failure(key)
                        self._json({"error": "invalid credentials"}, 401)
                elif path == "/api/logout":
                    raw = self.headers.get("Cookie", "")
                    if raw and sessions:
                        try:
                            ck = SimpleCookie(raw)
                            if "takcore_session" in ck:
                                sessions.destroy(ck["takcore_session"].value)
                        except Exception:  # noqa: BLE001
                            pass
                    self._json({"ok": True})
                elif path == "/api/users":
                    body = self._body_json() or {}
                    u = (body.get("username") or "").strip()
                    pw = body.get("password") or ""
                    role = body.get("role") or "viewer"
                    if not u or not pw or role not in authmod.ROLES:
                        self._json({"error": "username, password, valid role "
                                    "required"}, 400)
                        return
                    store.upsert_user(u, authmod.hash_password(pw), role)
                    self._json({"username": u, "role": role}, 201)
                elif path == "/api/users/role":
                    body = self._body_json() or {}
                    u = (body.get("username") or "").strip()
                    role = body.get("role") or ""
                    if not u or role not in authmod.ROLES:
                        self._json({"error": "username and valid role required"},
                                   400)
                        return
                    target = store.get_user(u)
                    if target is None:
                        self._json({"error": "no such user"}, 404)
                        return
                    # never leave the server with zero admins
                    if (target["role"] == "admin" and role != "admin"
                            and store.admin_count() <= 1):
                        self._json({"error": "cannot demote the last admin"}, 409)
                        return
                    store.set_role(u, role)
                    self._json({"username": u, "role": role})
                elif path == "/api/mediamtx/auth":
                    payload = self._body_json() or {}
                    if registry and registry.authorize(payload):
                        self._json({"ok": True})
                    else:
                        self._json({"ok": False}, 401)
                elif path == "/api/chat":
                    body = self._body_json() or {}
                    msg = (body.get("message") or "").strip()
                    if not msg:
                        self._json({"error": "message required"}, 400)
                        return
                    sender = (body.get("sender") or "TAKCORE").strip()
                    from .cot import build_geochat
                    hub.broadcast_raw(build_geochat(msg, sender_callsign=sender))
                    row = store.add_chat(sender, "takcore-server",
                                         "All Chat Rooms", msg)
                    hub._push_web({"kind": "chat", **row})
                    self._json(row, 201)
                elif path == "/api/streams":
                    if registry is None:
                        self._json({"error": "streams disabled"}, 503)
                        return
                    body = self._body_json() or {}
                    name = (body.get("name") or "").strip()
                    source = (body.get("source") or "").strip()
                    if not name or not source:
                        self._json({"error": "name and source required"}, 400)
                        return
                    result = registry.register_camera(
                        name, source,
                        device_uid=body.get("device_uid"),
                        lat=body.get("lat"), lon=body.get("lon"))
                    self._json(result, 201)
                elif path == "/api/tracks":
                    body = self._body_json() or {}
                    devices = body.get("devices") or []
                    if not isinstance(devices, list):
                        self._json({"error": "devices must be a list"}, 400)
                        return
                    # "all" is stored as an empty list on purpose: it must also
                    # cover devices that connect after the recording starts.
                    if body.get("all"):
                        devices = []
                    elif not devices:
                        self._json({"error": "select at least one device, or "
                                    "pass all=true"}, 400)
                        return
                    sess = self.address_string()
                    who = self._session()
                    row = store.start_track_session(
                        [str(d) for d in devices],
                        name=(body.get("name") or "").strip() or None,
                        created_by=(who or {}).get("username"))
                    hub._push_web({"kind": "track_started", **row})
                    log.info("track recording %s started by %s (devices=%s)",
                             row["id"], sess, row["devices"] or "all")
                    self._json(row, 201)
                elif path == "/api/tracks/stop":
                    body = self._body_json() or {}
                    try:
                        sid = int(body.get("id"))
                    except (TypeError, ValueError):
                        self._json({"error": "id required"}, 400)
                        return
                    known = [r for r in store.track_sessions() if r["id"] == sid]
                    if not known:
                        self._json({"error": "no such recording"}, 404)
                        return
                    if not store.stop_track_session(sid):
                        self._json({"error": "recording already stopped"}, 409)
                        return
                    row = [r for r in store.track_sessions() if r["id"] == sid][0]
                    hub._push_web({"kind": "track_stopped", **row})
                    log.info("track recording %s stopped by %s", sid,
                             self.address_string())
                    self._json(row)
                elif path == "/api/streams/record":
                    if registry is None:
                        self._json({"error": "streams disabled"}, 503)
                        return
                    body = self._body_json() or {}
                    spath = (body.get("path") or "").strip()
                    enabled = bool(body.get("enabled"))
                    if not spath:
                        self._json({"error": "path required"}, 400)
                        return
                    if registry.set_record(spath, enabled):
                        self._json({"path": spath, "recording": enabled})
                    else:
                        self._json({"error": "mediamtx unavailable"}, 502)
                elif path == "/api/enroll/tokens":
                    if enroll is None:
                        self._json({"error": "enrollment disabled (no TLS "
                                    "certs; run scripts/make-certs.sh)"}, 503)
                        return
                    body = self._body_json() or {}
                    result = enroll.create_token(
                        username=body.get("username"),
                        callsign=body.get("callsign"),
                        expires_hours=body.get("expires_hours", 24),
                        max_uses=body.get("max_uses"))
                    self._json(result, 201)
                elif path == "/api/enroll/bulk":
                    if enroll is None:
                        self._json({"error": "enrollment disabled"}, 503)
                        return
                    body = self._body_json() or {}
                    names = body.get("usernames") or []
                    if not isinstance(names, list) or not names:
                        self._json({"error": "usernames list required"}, 400)
                        return
                    self._json([enroll.create_token(username=str(n))
                                for n in names[:500]], 201)
                else:
                    self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001
                log.exception("error handling POST %s", path)
                self._json({"error": "internal"}, 500)

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authorized("DELETE", parsed.path):
                return
            try:
                if parsed.path == "/api/users":
                    q = parse_qs(parsed.query)
                    u = (q.get("username") or [""])[0]
                    if not u:
                        self._json({"error": "username required"}, 400)
                        return
                    sess = self._session()
                    if sess and sess["username"] == u:
                        self._json({"error": "you cannot delete your own "
                                    "account"}, 409)
                        return
                    target = store.get_user(u)
                    if target is None:
                        self._json({"error": "no such user"}, 404)
                        return
                    if target["role"] == "admin" and store.admin_count() <= 1:
                        self._json({"error": "cannot delete the last admin"}, 409)
                        return
                    store.delete_user(u)
                    self._json({"ok": True})
                elif parsed.path == "/api/devices":
                    q = parse_qs(parsed.query)
                    all_flag = (q.get("all") or [""])[0].lower() in (
                        "1", "true", "yes", "all")
                    if all_flag:
                        removed = store.delete_all_devices()
                        hub._push_web({"kind": "cleared"})
                        log.info("all devices cleared (%d) by %s", removed,
                                 self.address_string())
                        self._json({"ok": True, "removed": removed})
                    else:
                        uid = (q.get("uid") or [""])[0]
                        if not uid:
                            self._json({"error": "uid or all=1 required"}, 400)
                            return
                        if not store.delete_device(uid):
                            self._json({"error": "no such device"}, 404)
                            return
                        hub._push_web({"kind": "removed", "uid": uid})
                        log.info("device %s removed by %s", uid,
                                 self.address_string())
                        self._json({"ok": True})
                elif parsed.path == "/api/tracks":
                    q = parse_qs(parsed.query)
                    try:
                        sid = int((q.get("id") or [""])[0])
                    except ValueError:
                        self._json({"error": "id required"}, 400)
                        return
                    if not store.delete_track_session(sid):
                        self._json({"error": "no such recording"}, 404)
                        return
                    hub._push_web({"kind": "track_removed", "id": sid})
                    self._json({"ok": True})
                elif parsed.path == "/api/chat":
                    q = parse_qs(parsed.query)
                    all_flag = (q.get("all") or [""])[0].lower() in (
                        "1", "true", "yes", "all")
                    if all_flag:
                        n = store.clear_chat()
                        hub._push_web({"kind": "chat_cleared"})
                        log.info("chat history cleared (%d messages) by %s",
                                 n, self.address_string())
                        self._json({"ok": True, "removed": n})
                        return
                    mid = (q.get("id") or [""])[0]
                    if not mid.isdigit():
                        self._json({"error": "id or all=1 required"}, 400)
                        return
                    if not store.delete_chat(int(mid)):
                        self._json({"error": "no such message"}, 404)
                        return
                    hub._push_web({"kind": "chat_removed", "id": int(mid)})
                    self._json({"ok": True})
                elif parsed.path == "/api/alerts":
                    # Clearing from the web also cancels the alert on the
                    # devices: ATAK keeps showing a 911 until it sees the
                    # matching b-a-o-can, so a server-only clear would leave
                    # every phone still alarming.
                    from .cot import build_emergency_cancel
                    q = parse_qs(parsed.query)
                    all_flag = (q.get("all") or [""])[0].lower() in (
                        "1", "true", "yes", "all")
                    active = {a["uid"]: a for a in store.active_alerts()}
                    if all_flag:
                        targets = list(active)
                    else:
                        uid = (q.get("uid") or [""])[0]
                        if not uid:
                            self._json({"error": "uid or all=1 required"}, 400)
                            return
                        if uid not in active:
                            self._json({"error": "no active alert with that uid"},
                                       404)
                            return
                        targets = [uid]
                    for uid in targets:
                        store.clear_alert(uid)
                        hub.broadcast_raw(build_emergency_cancel(
                            uid, active[uid].get("callsign") or ""))
                        hub._push_web({"kind": "alert_clear", "uid": uid})
                    log.info("emergency cleared from the web UI: %s by %s",
                             targets, self.address_string())
                    self._json({"ok": True, "cleared": targets})
                elif parsed.path == "/api/streams":
                    q = parse_qs(parsed.query)
                    target = (q.get("path") or [""])[0]
                    if not target or registry is None:
                        self._json({"error": "path query param required"}, 400)
                        return
                    registry.remove(target)
                    self._json({"ok": True})
                elif parsed.path == "/api/enroll/tokens":
                    q = parse_qs(parsed.query)
                    username = (q.get("username") or [""])[0]
                    if not username:
                        self._json({"error": "username query param required"}, 400)
                        return
                    store.delete_enroll_user(username)
                    self._json({"ok": True})
                else:
                    self._json({"error": "not found"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001
                log.exception("error handling DELETE %s", parsed.path)
                self._json({"error": "internal"}, 500)

        # -- SSE ------------------------------------------------------------

        def _sse(self) -> None:
            q = hub.subscribe_web()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # initial snapshot so a fresh page is instantly populated
                for dev in store.devices():
                    self._sse_write(json.dumps({"kind": "position", **dev}))
                for al in store.active_alerts():
                    self._sse_write(json.dumps({"kind": "alert", **al}))
                while True:
                    try:
                        msg = q.get(timeout=15)
                        if msg is Hub.CLOSE:
                            break  # we fell behind; let EventSource reconnect
                        self._sse_write(msg)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe_web(q)

        def _sse_write(self, data: str) -> None:
            self.wfile.write(f"data: {data}\n\n".encode())
            self.wfile.flush()

        # -- static files ---------------------------------------------------

        def _static(self, path: str) -> None:
            if path in ("", "/"):
                path = "/index.html"
            # prevent path traversal
            safe = os.path.normpath(path).lstrip("/\\")
            full = os.path.join(web_dir, safe)
            if not os.path.abspath(full).startswith(os.path.abspath(web_dir)) \
                    or not os.path.isfile(full):
                self._json({"error": "not found"}, 404)
                return
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # never let browsers serve a stale UI (caused "login won't work"
            # after upgrades because old app.js was cached)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def start_web_server(hub: Hub, store: Store, port: int, web_dir: str,
                     registry: Optional[StreamRegistry] = None,
                     enroll: Optional[EnrollmentService] = None,
                     sessions: Optional[SessionManager] = None,
                     auth_enabled: bool = False,
                     throttle: Optional["authmod.LoginThrottle"] = None
                     ) -> ThreadingHTTPServer:
    handler = make_handler(hub, store, web_dir, registry, enroll,
                           sessions, auth_enabled, throttle)
    httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                              name="takcore-web")
    thread.start()
    log.info("web UI + API on http://0.0.0.0:%d", port)
    return httpd
