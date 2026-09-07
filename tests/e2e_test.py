#!/usr/bin/env python3
"""End-to-end test: boots a real takcore server and exercises every feature
added in Phases 1-4 over the actual wire (HTTP + CoT TCP).

    python3 server/tests/e2e_test.py

Uses ports 18080/18087/18446 and a temp database, so it can run while a
production instance (8080/8087/8446) is up. Stdlib only, like the server.

Covered: static UI + no-cache headers, auth gate, login/logout, roles
(admin vs viewer), device position ingest over TCP, roster API, track +
history (playback data), GeoChat both directions, emergency alerts raise +
cancel, SSE live feed, stream registry API, MediaMTX auth callback,
enrollment tokens + QR payload, truststore/enroll.zip downloads, config,
stats. MediaMTX- and cert-dependent checks are skipped cleanly when those
aren't available.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER_DIR = os.path.dirname(HERE)                 # .../server
sys.path.insert(0, HERE)

from simulate_atak import sa_event, chat_event, cot_time  # noqa: E402

HTTP_PORT = 18080
TCP_PORT = 18087
ENROLL_PORT = 18446
BASE = f"http://127.0.0.1:{HTTP_PORT}"
ADMIN_PW = "e2e-secret"

passed, failed, skipped = [], [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))


def skip(name: str, why: str) -> None:
    skipped.append(name)
    print(f"  SKIP  {name}  ({why})")


def req(path: str, method: str = "GET", body: dict | None = None,
        cookie: str = "", raw: bool = False):
    """Returns (status, parsed-json-or-bytes, set-cookie-header)."""
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if body is not None:
        r.add_header("Content-Type", "application/json")
    if cookie:
        r.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            payload = resp.read()
            ck = resp.headers.get("Set-Cookie", "")
            if raw:
                return resp.status, payload, resp.headers
            return resp.status, json.loads(payload) if payload else None, ck
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = payload
        return e.code, parsed, ""


def wait_for(fn, timeout: float = 10.0, interval: float = 0.3):
    """Polls fn() until it returns a truthy value or the deadline passes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            v = fn()
            if v:
                return v
        except Exception:
            pass
        time.sleep(interval)
    return None


def recv_until(sock: socket.socket, needle: bytes, timeout: float = 10.0) -> bytes:
    """Reads from sock until needle appears or timeout; returns buffer."""
    sock.settimeout(0.5)
    buf = b""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if needle in buf:
            return buf
        try:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            continue
    return buf


def emergency_event(uid: str, callsign: str, lat: float, lon: float,
                    cancel: bool = False) -> bytes:
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=10)
    etype = "b-a-o-can" if cancel else "b-a-o-tbl"
    attr = ' cancel="true"' if cancel else ' type="911 Alert"'
    return (
        f'<event version="2.0" uid="{uid}-9-1-1" type="{etype}" how="h-e" '
        f'time="{cot_time(now)}" start="{cot_time(now)}" stale="{cot_time(stale)}">'
        f'<point lat="{lat}" lon="{lon}" hae="0" ce="10" le="10"/>'
        f'<detail><emergency{attr}>{callsign}</emergency>'
        f'<link uid="{uid}" relation="p-p" type="a-f-G-U-C"/></detail></event>'
    ).encode()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="takcore-e2e-")
    certs_ok = (os.path.isfile(os.path.join(SERVER_DIR, "certs", "server.pem"))
                and os.path.isfile(os.path.join(SERVER_DIR, "certs", "server.key")))
    env = dict(os.environ,
               HTTP_PORT=str(HTTP_PORT), TAK_TCP_PORT=str(TCP_PORT),
               TAK_TLS_PORT="0", ENROLL_PORT=str(ENROLL_PORT) if certs_ok else "0",
               DB_PATH=os.path.join(tmp, "e2e.db"),
               AUTH_ENABLED="1", ADMIN_USER="admin", ADMIN_PASSWORD=ADMIN_PW,
               SERVER_HOST="127.0.0.1",
               MEDIAMTX_API=os.environ.get("MEDIAMTX_API", "http://127.0.0.1:19997"),
               LOG_LEVEL="WARNING")
    proc = subprocess.Popen([sys.executable, "-m", "takcore"], cwd=SERVER_DIR,
                            env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    try:
        return run_checks(certs_ok)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def run_checks(certs_ok: bool) -> int:
    print("== waiting for server ==")
    up = wait_for(lambda: req("/api/me")[0] == 200, timeout=15)
    if not up:
        print("FATAL: server did not come up on port", HTTP_PORT)
        return 1

    print("== Phase 4: web auth ==")
    st, body, _ = req("/api/me")
    check("auth required advertised", st == 200 and body.get("auth_required") is True)
    st, body, _ = req("/api/devices")
    check("protected API rejects anonymous (401)", st == 401)
    st, body, _ = req("/api/login", "POST", {"username": "admin", "password": "wrong"})
    check("wrong password rejected (401)", st == 401)
    st, body, ck = req("/api/login", "POST", {"username": "admin", "password": ADMIN_PW})
    admin_ck = ck.split(";")[0] if ck else ""
    check("admin login sets session cookie", st == 200 and "takcore_session=" in admin_ck)
    st, body, _ = req("/api/me", cookie=admin_ck)
    check("session recognised as admin", st == 200 and body.get("role") == "admin")

    st, body, _ = req("/api/users", "POST",
                      {"username": "eve", "password": "evepw", "role": "viewer"},
                      cookie=admin_ck)
    check("admin can create user", st == 201)
    st, body, ck = req("/api/login", "POST", {"username": "eve", "password": "evepw"})
    viewer_ck = ck.split(";")[0] if ck else ""
    check("viewer login works", st == 200)
    st, body, _ = req("/api/users", "POST",
                      {"username": "x", "password": "y", "role": "viewer"},
                      cookie=viewer_ck)
    check("viewer blocked from admin action (403)", st == 403)

    print("== Phase 1: static UI + CoT ingest ==")
    st, payload, headers = req("/", raw=True)
    check("index.html served", st == 200 and b"<html" in payload.lower())
    check("no-cache headers on static files",
          "no-store" in (headers.get("Cache-Control") or ""))

    uid, cs = "E2E-0001", "E2E-Alpha"
    dev = socket.create_connection(("127.0.0.1", TCP_PORT), timeout=5)
    dev.sendall(sa_event(uid, cs, "Cyan", "Team Member", 43.4193, -80.5404, 90.0, 1.2))
    found = wait_for(lambda: any(d["uid"] == uid
                                 for d in req("/api/devices", cookie=admin_ck)[1]))
    check("device appears in roster after CoT position", bool(found))
    st, body, _ = req(f"/api/devices/{uid}", cookie=admin_ck)
    check("device detail fields parsed",
          st == 200 and body.get("callsign") == cs and body.get("team") == "Cyan")

    print("== Phase 4: track history / playback data ==")
    for i in range(3):
        dev.sendall(sa_event(uid, cs, "Cyan", "Team Member",
                             43.4193 + i * 0.001, -80.5404, 90.0, 1.2))
        time.sleep(0.4)
    got = wait_for(lambda: len(req(f"/api/devices/{uid}/track",
                                   cookie=admin_ck)[1] or []) >= 2)
    check("breadcrumb track recorded (>=2 points)", bool(got))
    st, body, _ = req("/api/history?minutes=60", cookie=admin_ck)
    check("history endpoint returns playback rows",
          st == 200 and isinstance(body, list) and len(body) >= 2)

    print("== Phase 4: GeoChat both directions ==")
    dev.sendall(chat_event(uid, cs, "hello from the field"))
    got = wait_for(lambda: any("hello from the field" in (m.get("message") or "")
                               for m in req("/api/chat", cookie=admin_ck)[1] or []))
    check("device chat reaches web history", bool(got))
    st, body, _ = req("/api/chat", "POST",
                      {"message": "ack from HQ", "sender": "HQ"}, cookie=admin_ck)
    check("web chat POST accepted", st == 201)
    buf = recv_until(dev, b"ack from HQ")
    check("web chat delivered to device as CoT", b"ack from HQ" in buf and b"b-t-f" in buf)

    print("== Phase 4: emergency alerts ==")
    dev.sendall(emergency_event(uid, cs, 43.42, -80.54))
    got = wait_for(lambda: len(req("/api/alerts", cookie=admin_ck)[1] or []) >= 1)
    check("911 beacon raises active alert", bool(got))
    dev.sendall(emergency_event(uid, cs, 43.42, -80.54, cancel=True))
    gone = wait_for(lambda: len(req("/api/alerts", cookie=admin_ck)[1] or []) == 0)
    check("cancel clears the alert", bool(gone))

    print("== Phase 4: SSE live feed ==")
    conn = http.client.HTTPConnection("127.0.0.1", HTTP_PORT, timeout=5)
    conn.request("GET", "/api/stream", headers={"Cookie": admin_ck})
    resp = conn.getresponse()
    sse_ok = resp.status == 200
    data = b""
    if sse_ok:
        dev.sendall(sa_event(uid, cs, "Cyan", "Team Member", 43.43, -80.55, 10.0, 2.0))
        deadline = time.time() + 8
        while time.time() < deadline and uid.encode() not in data:
            try:
                data += resp.read1(65536) or b""
            except Exception:
                break
    conn.close()
    check("SSE streams live position events", sse_ok and uid.encode() in data)

    print("== Phase 2: video / stream registry ==")
    st, body, _ = req("/api/mediamtx/auth", "POST",
                      {"action": "read", "path": f"live/{uid}", "query": ""})
    check("MediaMTX auth callback answers", st in (200, 401))
    st, body, _ = req("/api/streams", cookie=admin_ck)
    check("stream list endpoint", st == 200 and isinstance(body, list))
    st, body, _ = req("/api/streams", "POST",
                      {"name": "e2e-cam", "source": "rtsp://127.0.0.1:9/void",
                       "lat": 43.4, "lon": -80.5}, cookie=admin_ck)
    if st == 200:
        check("IP camera registered", True)
        st2, _, _ = req("/api/streams?path=" +
                        (body.get("path") if isinstance(body, dict) else "e2e-cam"),
                        "DELETE", cookie=admin_ck)
        check("IP camera removed", st2 == 200)
    else:
        skip("IP camera register/remove", "MediaMTX not running")

    print("== Phase 3: enrollment ==")
    if not certs_ok:
        skip("enrollment token lifecycle", "no TLS certs (run scripts/make-certs.sh)")
        skip("truststore/enroll.zip downloads", "no TLS certs")
    else:
        st, body, _ = req("/api/enroll/tokens", "POST",
                          {"callsign": "E2E-QR", "expires_hours": 1}, cookie=admin_ck)
        tok_ok = st == 201 and body.get("username") and body.get("token")
        check("enrollment token issued", bool(tok_ok))
        qr_user = body.get("username") if tok_ok else ""
        qr_token = body.get("token") if tok_ok else ""
        st, body, _ = req("/api/enroll/tokens", cookie=admin_ck)
        check("token appears in list",
              st == 200 and any(t.get("callsign") == "E2E-QR" for t in body or []))
        st, body, _ = req("/api/enroll/bulk", "POST",
                          {"usernames": ["e2e-b1", "e2e-b2"]}, cookie=admin_ck)
        check("bulk enrollment", st == 201)
        st, payload, _ = req("/truststore.p12", raw=True)
        check("truststore.p12 public download", st == 200 and len(payload) > 100)
        # softcert bundle contains a client identity -> must be token-gated
        st, payload, _ = req("/enroll.zip?host=127.0.0.1&callsign=E2E-QR",
                             raw=True)
        check("enroll.zip softcert refused without a token", st == 403)
        qr_url = (f"/enroll.zip?host=127.0.0.1&callsign=E2E-QR"
                  f"&username={qr_user}&token={qr_token}")
        st, payload, _ = req(qr_url, raw=True)
        check("enroll.zip (softcert data package) builds with a valid token",
              st == 200 and payload[:2] == b"PK")
        # the QR must keep working: ATAK fetches it more than once and users
        # retry, so the token must NOT be single-use for softcert downloads
        st2, payload2, _ = req(qr_url, raw=True)
        check("enroll.zip token is reusable (not burned on first download)",
              st2 == 200 and payload2[:2] == b"PK")
        st, body, _ = req("/api/enroll/tokens?username=e2e-b1", "DELETE",
                          cookie=admin_ck)
        check("token revoked", st == 200)

    print("== misc APIs ==")
    st, body, _ = req("/api/config", cookie=admin_ck)
    check("config exposes server_host", st == 200 and "server_host" in body)
    st, body, _ = req("/api/stats", cookie=admin_ck)
    check("stats endpoint", st == 200)
    st, _, hdrs = req("/api/devices", cookie=admin_ck, raw=True)
    check("no CORS wildcard on API responses",
          st == 200 and "Access-Control-Allow-Origin" not in hdrs)

    print("== logout ==")
    st, body, _ = req("/api/logout", "POST", cookie=admin_ck)
    check("logout accepted", st == 200)
    st, body, _ = req("/api/devices", cookie=admin_ck)
    check("session invalid after logout (401)", st == 401)

    dev.close()
    print(f"\n{len(passed)} passed, {len(failed)} failed, {len(skipped)} skipped")
    if failed:
        print("FAILED:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
