#!/opt/rtak/runtime/python/bin/python3
"""End-to-end functional test of an INSTALLED RTAK Server on Linux.

Runs with the bundled interpreter, uses only the standard library, and drives
the real paths a phone would: log in, mint an enrollment token, get a client
certificate signed by the Marti API, connect over mTLS on 8089, then report
positions, a chat message and a 911 emergency - and verify the server stored
all of it.

    /opt/rtak/runtime/python/bin/python3 linux_functional_test.py
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PREFIX = "/opt/rtak"
ENVF = "/etc/rtak/rtak.env"
OPENSSL = f"{PREFIX}/bin/openssl"
API = "http://127.0.0.1:8080"

PASS, FAIL = [], []


def ok(m):
    PASS.append(m)
    print(f"  \033[1;32mPASS\033[0m {m}")


def bad(m):
    FAIL.append(m)
    print(f"  \033[1;31mFAIL\033[0m {m}")


def head(m):
    print(f"\n\033[1;36m{m}\033[0m")


def env():
    d = {}
    with open(ENVF) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    return d


E = env()
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(path, data=None, method=None):
    req = urllib.request.Request(
        API + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"},
        method=method or ("POST" if data is not None else "GET"))
    try:
        with opener.open(req, timeout=10) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, body


def cot_time(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def sa(uid, callsign, lat, lon, team="Cyan", role="Team Member"):
    now = datetime.now(timezone.utc)
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{uid}" type="a-f-G-U-C" how="m-g" '
        f'time="{cot_time(now)}" start="{cot_time(now)}" '
        f'stale="{cot_time(now + timedelta(seconds=120))}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="100" ce="5" le="9999999"/>'
        f'<detail><takv os="34" version="5.2" device="LINUX-TEST" platform="ATAK"/>'
        f'<contact callsign="{callsign}" endpoint="*:-1:stcp"/>'
        f'<__group role="{role}" name="{team}"/>'
        f'<status battery="88"/></detail></event>').encode()


def chat(uid, callsign, msg):
    now = datetime.now(timezone.utc)
    return (
        f'<event version="2.0" uid="GeoChat.{uid}.All.1" type="b-t-f" '
        f'how="h-g-i-g-o" time="{cot_time(now)}" start="{cot_time(now)}" '
        f'stale="{cot_time(now + timedelta(minutes=5))}">'
        f'<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>'
        f'<detail><__chat chatroom="All Chat Rooms" id="All Chat Rooms" '
        f'senderCallsign="{callsign}"><chatgrp uid0="{uid}" '
        f'uid1="All Chat Rooms" id="All Chat Rooms"/></__chat>'
        f'<remarks source="BAO.F.ATAK.{uid}">{msg}</remarks></detail></event>').encode()


def emergency(uid, callsign, lat, lon):
    now = datetime.now(timezone.utc)
    return (
        f'<event version="2.0" uid="{uid}-9-1-1" type="b-a-o-tbl" how="m-g" '
        f'time="{cot_time(now)}" start="{cot_time(now)}" '
        f'stale="{cot_time(now + timedelta(minutes=10))}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="10" ce="5" le="9999999"/>'
        f'<detail><emergency type="911 Alert">{callsign}</emergency>'
        f'<contact callsign="{callsign}"/></detail></event>').encode()


def main():
    head("1. Web API is up")
    code, cfg = call("/api/config")
    if code in (200, 401):
        ok(f"web API answering (HTTP {code})")
    else:
        bad(f"web API returned {code}")
        return report()

    head("2. Admin login")
    code, _ = call("/api/login", {"username": E.get("ADMIN_USER", "admin"),
                                  "password": E.get("ADMIN_PASSWORD", "")})
    ok("logged in as admin") if code == 200 else bad(f"login failed (HTTP {code})")

    code, cfg = call("/api/config")
    if code == 200 and cfg:
        ok(f"SERVER_HOST advertised as '{cfg.get('server_host')}'")

    head("3. Wipe any previous test devices")
    call("/api/devices?all=1", method="DELETE")
    code, devs = call("/api/devices")
    ok("device list empty") if devs == [] else bad(f"devices not empty: {devs}")

    head("4. Certificate enrollment (the phone's path)")
    code, tok = call("/api/enroll/tokens", {"callsign": "LINUX-TEST"})
    if code != 201 or not tok:
        bad(f"could not create enrollment token (HTTP {code}: {tok})")
        return report()
    ok(f"enrollment token issued for user {tok['username']}")

    work = tempfile.mkdtemp()
    key = os.path.join(work, "c.key")
    csr = os.path.join(work, "c.csr")
    crt = os.path.join(work, "c.pem")
    ca0 = os.path.join(work, "ca0.pem")
    r = subprocess.run([OPENSSL, "req", "-new", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-subj", f"/CN={tok['username']}", "-out", csr],
                       capture_output=True)
    ok("client key + CSR generated with the bundled openssl") if r.returncode == 0 \
        else bad(f"openssl req failed: {r.stderr.decode()[:200]}")

    import base64
    ctx = ssl._create_unverified_context()
    auth = base64.b64encode(f"{tok['username']}:{tok['token']}".encode()).decode()
    req = urllib.request.Request(
        "https://127.0.0.1:8446/Marti/api/tls/signClient/v2?clientUid=LINUX&version=5.2",
        data=open(csr, "rb").read(),
        headers={"Authorization": f"Basic {auth}"}, method="POST")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            signed = json.loads(r.read().decode())
        ok("Marti API signed the client certificate")
    except Exception as e:
        bad(f"signClient failed: {e}")
        return report()

    for field, path in (("signedCert", crt), ("ca0", ca0)):
        b = signed[field]
        pem = ("-----BEGIN CERTIFICATE-----\n"
               + "\n".join(b[i:i + 64] for i in range(0, len(b), 64))
               + "\n-----END CERTIFICATE-----\n")
        open(path, "w").write(pem)
    r = subprocess.run([OPENSSL, "verify", "-CAfile", ca0, crt], capture_output=True)
    ok("issued certificate verifies against the CA") if r.returncode == 0 \
        else bad(f"verify failed: {r.stderr.decode()[:200]}")

    head("5. Devices connect over mTLS on :8089")
    ctx2 = ssl._create_unverified_context()
    ctx2.load_cert_chain(crt, key)
    socks = []
    for i, (uid, cs, team, role) in enumerate([
            ("LNX-0001", "LNX-0001", "Red", "Team Lead"),
            ("LNX-0002", "LNX-0002", "Blue", "Medic")]):
        try:
            s = ctx2.wrap_socket(socket.create_connection(("127.0.0.1", 8089), timeout=10))
            s.sendall(sa(uid, cs, 45.4215 + i * 0.01, -75.6972 + i * 0.01, team, role))
            socks.append((s, uid, cs))
            ok(f"{uid} connected over mTLS and sent a position")
        except Exception as e:
            bad(f"{uid} could not connect: {e}")

    head("6. Tracking, chat and emergency")
    for n in range(4):
        time.sleep(0.7)
        for i, (s, uid, cs) in enumerate(socks):
            try:
                s.sendall(sa(uid, cs, 45.4215 + i * 0.01 + n * 0.001,
                             -75.6972 + i * 0.01 + n * 0.001))
            except Exception:
                pass
    if socks:
        socks[0][0].sendall(chat(socks[0][1], socks[0][2], "linux install radio check"))
        socks[0][0].sendall(emergency(socks[0][1], socks[0][2], 45.4215, -75.6972))
    time.sleep(2.0)

    code, devs = call("/api/devices")
    (ok(f"{len(devs)} device(s) on the server") if devs and len(devs) >= 2
     else bad(f"expected 2 devices, got {devs}"))

    code, hist = call("/api/history?minutes=60")
    n = len(hist) if isinstance(hist, list) else 0
    ok(f"{n} tracking breadcrumbs stored") if n >= 4 else bad(f"only {n} breadcrumbs")

    code, msgs = call("/api/chat")
    hit = [m for m in (msgs or []) if "radio check" in str(m.get("message", ""))]
    ok("chat message stored and readable") if hit else bad(f"chat not stored: {msgs}")

    code, alerts = call("/api/alerts")
    ok(f"emergency alert active ({alerts[0].get('callsign')})") if alerts \
        else bad("emergency alert not recorded")

    head("7. Video engine")
    try:
        with urllib.request.urlopen("http://127.0.0.1:9997/v3/paths/list", timeout=6) as r:
            json.load(r)
        ok("MediaMTX control API responding")
    except Exception as e:
        bad(f"MediaMTX API not responding: {e}")
    for p, label in ((8554, "RTSP"), (8889, "WebRTC")):
        with socket.socket() as s:
            s.settimeout(2)
            ok(f"{label} port {p} listening") if s.connect_ex(("127.0.0.1", p)) == 0 \
                else bad(f"{label} port {p} not listening")

    head("8. ATAK truststore (needs the legacy OpenSSL provider)")
    ts = "/etc/rtak/certs/truststore.p12"
    if os.path.exists(ts):
        r = subprocess.run([OPENSSL, "pkcs12", "-info", "-in", ts, "-nokeys",
                            "-passin", "pass:atakatak", "-legacy"], capture_output=True)
        ok("truststore.p12 readable in the ATAK-compatible format") if r.returncode == 0 \
            else bad(f"truststore unreadable: {r.stderr.decode()[:200]}")
    else:
        bad("truststore.p12 was not generated")

    for s, _, _ in socks:
        try:
            s.close()
        except Exception:
            pass
    return report()


def report():
    print("\n" + "=" * 58)
    print(f"  PASSED: {len(PASS)}    FAILED: {len(FAIL)}")
    print("=" * 58)
    if FAIL:
        for f in FAIL:
            print(f"  - {f}")
        return 1
    print("  \033[1;32mEverything works on this Linux install.\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
