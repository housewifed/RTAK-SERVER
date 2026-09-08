#!/usr/bin/env python3
"""Full system test of an INSTALLED RTAK Server.

Drives every user-visible subsystem against a running server: authentication,
users and roles, certificate enrollment, devices over mTLS, tracking, chat,
911, cameras, video ingest in every supported format, WebRTC playback tickets,
recording and playback, and the ATAK truststore.

Standard library only, plus ffmpeg/ffprobe for the video sections (skipped
with a clear note when they are absent).

    # on the server itself - reads /etc/rtak/rtak.env for the port and password
    /opt/rtak/runtime/python/bin/python3 full_system_check.py

    # from a machine that has ffmpeg (the server usually does not)
    python3 full_system_check.py --api http://192.168.2.101:8081 \
        --user admin --password 'secret' --media-host 192.168.2.101

Everything it creates (users, devices, camera paths, recordings) is removed
again at the end unless --keep is given. It never touches data it did not
create: no bulk device wipe, no database edits.
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ENVF = "/etc/rtak/rtak.env"
PREFIX = "/opt/rtak"
TAG = "fullcheck"           # every object this test creates carries this tag

PASS: list = []
FAIL: list = []
SKIP: list = []

C_G, C_R, C_Y, C_B, C_D, C_0 = (
    "\033[1;32m", "\033[1;31m", "\033[1;33m", "\033[1;36m", "\033[2m", "\033[0m")


def ok(m):
    PASS.append(m)
    print(f"  {C_G}PASS{C_0} {m}")


def bad(m):
    FAIL.append(m)
    print(f"  {C_R}FAIL{C_0} {m}")


def skip(m):
    SKIP.append(m)
    print(f"  {C_Y}SKIP{C_0} {m}")


def head(m):
    print(f"\n{C_B}{m}{C_0}")


def note(m):
    print(f"       {C_D}{m}{C_0}")


def read_env(path=ENVF) -> dict:
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k] = v
    except OSError:
        pass
    return out


# --------------------------------------------------------------- HTTP ------
class Api:
    """Cookie-aware JSON client for one logged-in session."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, path, data=None, method=None, timeout=15):
        url = self.base + path
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with self.op.open(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
                try:
                    return r.status, json.loads(raw) if raw else None
                except json.JSONDecodeError:
                    return r.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return e.code, raw
        except Exception as e:  # noqa: BLE001
            return 0, str(e)

    def login(self, user, pw):
        return self.call("/api/login", {"username": user, "password": pw})


# ----------------------------------------------------------- CoT builders --
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
        f'<detail><takv os="34" version="5.2" device="FULLCHECK" platform="ATAK"/>'
        f'<contact callsign="{callsign}" endpoint="*:-1:stcp"/>'
        f'<__group role="{role}" name="{team}"/>'
        f'<status battery="88"/></detail></event>').encode()


def geochat(uid, callsign, msg):
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
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="100" ce="5" le="9999999"/>'
        f'<detail><emergency type="911 Alert">{callsign}</emergency>'
        f'<contact callsign="{callsign}"/><link uid="{uid}" '
        f'type="a-f-G-U-C" relation="p-p"/></detail></event>').encode()


def port_open(host, port, timeout=3.0):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, int(port))) == 0


# ------------------------------------------------------------ main test ----
def main() -> int:
    env = read_env()
    ap = argparse.ArgumentParser(description="Full system test of an installed RTAK Server")
    ap.add_argument("--api", default=f"http://127.0.0.1:{env.get('HTTP_PORT', '8080')}")
    ap.add_argument("--user", default=env.get("ADMIN_USER", "admin"))
    ap.add_argument("--password", default=env.get("ADMIN_PASSWORD", ""))
    ap.add_argument("--media-host", default=None,
                    help="host for RTSP/RTMP/SRT/WebRTC (default: the API host)")
    ap.add_argument("--enroll-port", type=int, default=int(env.get("ENROLL_PORT", 8446)))
    ap.add_argument("--cot-port", type=int, default=int(env.get("TAK_TLS_PORT", 8089)))
    ap.add_argument("--publish-token", default=env.get("PUBLISH_TOKEN", ""))
    ap.add_argument("--no-video", action="store_true", help="skip ffmpeg sections")
    ap.add_argument("--keep", action="store_true", help="leave test objects behind")
    args = ap.parse_args()

    api_host = urllib.parse.urlparse(args.api).hostname or "127.0.0.1"
    mhost = args.media_host or api_host
    local = os.access(ENVF, os.R_OK)
    openssl = f"{PREFIX}/bin/openssl" if os.path.exists(f"{PREFIX}/bin/openssl") \
        else shutil.which("openssl")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    work = tempfile.mkdtemp(prefix="rtak-fullcheck-")
    created = {"users": [], "devices": [], "paths": []}
    procs: list = []
    socks: list = []

    print(f"\n{C_B}RTAK Server - full system check{C_0}")
    note(f"api {args.api}   media {mhost}   mode {'local' if local else 'remote'}"
         f"   ffmpeg {'yes' if ffmpeg else 'no'}")

    a = Api(args.api)

    # ---------------------------------------------------------------------
    head("1. Reachability and configuration")
    # Every /api/ path except login needs a session, so 401 here still proves
    # the server is up and enforcing auth.
    code, _ = a.call("/api/config")
    if code in (200, 401, 403):
        ok(f"web API answering (HTTP {code})")
    else:
        bad(f"web API not answering ({code})")
        return report()

    for port, label in ((args.cot_port, "CoT over TLS"),
                        (args.enroll_port, "certificate enrollment"),
                        (8554, "RTSP ingest"), (1935, "RTMP ingest"),
                        (8889, "WebRTC")):
        ok(f"port {port} open ({label})") if port_open(mhost, port) \
            else bad(f"port {port} closed ({label})")

    # ---------------------------------------------------------------------
    head("2. Authentication")
    anon = Api(args.api)
    code, _ = anon.call("/api/devices")
    ok("unauthenticated API access refused (401)") if code == 401 \
        else bad(f"unauthenticated /api/devices returned {code}, expected 401")

    code, _ = anon.login(args.user, "definitely-the-wrong-password")
    ok("wrong password refused") if code in (401, 403) \
        else bad(f"wrong password returned {code}")

    code, who = a.login(args.user, args.password)
    if code != 200:
        bad(f"admin login failed ({code}: {who})")
        return report()
    ok(f"admin login OK as '{args.user}'")

    code, cfg = a.call("/api/config")
    if code == 200 and isinstance(cfg, dict):
        ok("configuration readable once logged in")
        note(f"server_host={cfg.get('server_host')}")
    else:
        bad(f"/api/config returned {code} for an admin session")

    code, me = a.call("/api/me")
    ok(f"session identifies as {me.get('username')} role={me.get('role')}") \
        if code == 200 and me.get("role") == "admin" \
        else bad(f"/api/me unexpected: {code} {me}")

    # ---------------------------------------------------------------------
    head("3. Users and roles")
    u_view, u_oper = f"{TAG}-viewer", f"{TAG}-operator"
    for name, role in ((u_view, "viewer"), (u_oper, "operator")):
        code, r = a.call("/api/users", {"username": name, "password": "Test!1234", "role": role},
                         method="POST")
        if code == 201:
            created["users"].append(name)
            ok(f"created {role} user '{name}'")
        else:
            bad(f"could not create {role} user: {code} {r}")

    code, users = a.call("/api/users")
    ok(f"user list readable ({len(users)} users)") if code == 200 and isinstance(users, list) \
        else bad(f"user list failed: {code} {users}")

    viewer = Api(args.api)
    code, _ = viewer.login(u_view, "Test!1234")
    ok("viewer can log in") if code == 200 else bad(f"viewer login failed: {code}")
    code, _ = viewer.call("/api/users", {"username": "x", "password": "y", "role": "admin"},
                          method="POST")
    ok("viewer denied user creation (403)") if code == 403 \
        else bad(f"viewer creating a user returned {code}, expected 403")
    code, _ = viewer.call("/api/chat", {"message": "viewer should not post"}, method="POST")
    ok("viewer denied chat POST (403)") if code == 403 \
        else bad(f"viewer chat POST returned {code}, expected 403")
    code, _ = viewer.call("/api/devices")
    ok("viewer can read devices") if code == 200 else bad(f"viewer read denied: {code}")

    operator = Api(args.api)
    code, _ = operator.login(u_oper, "Test!1234")
    code, _ = operator.call("/api/chat", {"message": f"{TAG} operator hello"}, method="POST")
    ok("operator allowed chat POST") if code == 201 \
        else bad(f"operator chat POST returned {code}, expected 201")
    code, _ = operator.call("/api/users", {"username": "x", "password": "y", "role": "admin"},
                            method="POST")
    ok("operator denied user creation (403)") if code == 403 \
        else bad(f"operator creating a user returned {code}, expected 403")

    code, r = a.call("/api/users/role", {"username": args.user, "role": "viewer"}, method="POST")
    ok("last admin cannot be demoted (409)") if code == 409 \
        else bad(f"demoting the last admin returned {code}, expected 409")

    code, r = a.call("/api/users/role", {"username": u_view, "role": "operator"}, method="POST")
    ok("role change applied") if code == 200 and r.get("role") == "operator" \
        else bad(f"role change failed: {code} {r}")

    # ---------------------------------------------------------------------
    head("4. Certificate enrollment (the phone's path)")
    code, tok = a.call("/api/enroll/tokens", {"callsign": f"{TAG}-phone"}, method="POST")
    if code != 201 or not isinstance(tok, dict):
        bad(f"enrollment token not issued: {code} {tok}")
        return report(work, procs, socks)
    enroll_user = tok["username"]
    ok(f"enrollment token issued for {enroll_user}")

    key, csr = os.path.join(work, "c.key"), os.path.join(work, "c.csr")
    crt, ca0 = os.path.join(work, "c.pem"), os.path.join(work, "ca0.pem")
    r = subprocess.run([openssl, "req", "-new", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-subj", f"/CN={tok['username']}", "-out", csr],
                       capture_output=True)
    ok("client key + CSR generated") if r.returncode == 0 \
        else bad(f"openssl req failed: {r.stderr.decode()[:160]}")

    ctx = ssl._create_unverified_context()
    auth = base64.b64encode(f"{tok['username']}:{tok['token']}".encode()).decode()
    req = urllib.request.Request(
        f"https://{mhost}:{args.enroll_port}/Marti/api/tls/signClient/v2"
        f"?clientUid={TAG}&version=5.2",
        data=open(csr, "rb").read(),
        headers={"Authorization": f"Basic {auth}"}, method="POST")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=20) as r2:
            signed = json.loads(r2.read().decode())
        ok("Marti API signed the client certificate")
    except Exception as e:  # noqa: BLE001
        bad(f"signClient failed: {e}")
        return report(work, procs, socks)

    for field, path in (("signedCert", crt), ("ca0", ca0)):
        b = signed[field]
        open(path, "w").write("-----BEGIN CERTIFICATE-----\n"
                              + "\n".join(b[i:i + 64] for i in range(0, len(b), 64))
                              + "\n-----END CERTIFICATE-----\n")
    r = subprocess.run([openssl, "verify", "-CAfile", ca0, crt], capture_output=True)
    ok("issued certificate verifies against the CA") if r.returncode == 0 \
        else bad(f"verify failed: {r.stderr.decode()[:160]}")

    for ep, label in (("/Marti/api/tls/config", "TLS config"),
                      ("/Marti/api/version/config", "version config"),
                      ("/Marti/api/clientEndPoints", "client endpoints")):
        try:
            rq = urllib.request.Request(f"https://{mhost}:{args.enroll_port}{ep}",
                                        headers={"Authorization": f"Basic {auth}"})
            with urllib.request.urlopen(rq, context=ctx, timeout=15) as r3:
                r3.read()
            ok(f"Marti {label} endpoint answers")
        except Exception as e:  # noqa: BLE001
            bad(f"Marti {label} failed: {e}")

    # enroll.zip has two modes. mode=enroll carries CA trust only and is open;
    # the default softcert mode contains a ready-to-use client identity and so
    # requires a valid enrollment token - anonymous access must be refused.
    code, _ = a.call("/enroll.zip")
    ok("softcert data package refused without a token (403)") if code == 403 \
        else bad(f"anonymous /enroll.zip returned {code}, expected 403")

    q = urllib.parse.urlencode({"username": enroll_user, "token": tok["token"],
                                "callsign": f"{TAG}-phone"})
    code, body = a.call(f"/enroll.zip?{q}")
    ok(f"softcert data package downloadable with a token ({len(body or '')} bytes)") \
        if code == 200 and body else bad(f"softcert /enroll.zip returned {code}")

    code, body = a.call("/enroll.zip?mode=enroll")
    ok("token-prompt data package downloadable") if code == 200 and body \
        else bad(f"/enroll.zip?mode=enroll returned {code}")

    code, body = a.call("/ca.mobileconfig")
    ok("iTAK mobileconfig downloadable") if code == 200 and body \
        else bad(f"/ca.mobileconfig returned {code}")

    # ---------------------------------------------------------------------
    head("5. Devices connect over mTLS")
    ctx2 = ssl._create_unverified_context()
    ctx2.load_cert_chain(crt, key)
    devices = [(f"{TAG}-0001", "Red", "Team Lead"), (f"{TAG}-0002", "Blue", "Medic")]
    for i, (uid, team, role) in enumerate(devices):
        try:
            s = ctx2.wrap_socket(socket.create_connection((mhost, args.cot_port), timeout=15))
            s.sendall(sa(uid, uid, 45.4215 + i * 0.01, -75.6972 + i * 0.01, team, role))
            socks.append(s)
            created["devices"].append(uid)
            ok(f"{uid} connected over mTLS and sent a position")
        except Exception as e:  # noqa: BLE001
            bad(f"{uid} could not connect: {e}")

    try:
        plain = socket.create_connection((mhost, args.cot_port), timeout=8)
        plain.sendall(b"<event/>")
        time.sleep(1.0)
        data = plain.recv(64)
        plain.close()
        ok("plain TCP to the mTLS port is rejected") if not data \
            else bad("plain TCP got a response from the mTLS CoT port")
    except (ssl.SSLError, OSError):
        ok("plain TCP to the mTLS port is rejected")

    # ---------------------------------------------------------------------
    head("6. Tracking, chat, emergency and live events")
    events = []

    def sse_reader():
        try:
            with a.op.open(args.api + "/api/stream", timeout=25) as r:
                for line in r:
                    line = line.decode("utf-8", "replace").strip()
                    if line.startswith("data:"):
                        events.append(line[5:].strip())
                        if len(events) >= 2:
                            return
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=sse_reader, daemon=True)
    t.start()
    time.sleep(1.0)

    for i in range(5):
        for j, s in enumerate(socks):
            try:
                s.sendall(sa(devices[j][0], devices[j][0],
                             45.4215 + j * 0.01 + i * 0.001,
                             -75.6972 + j * 0.01 + i * 0.001))
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.4)

    if socks:
        socks[0].sendall(geochat(devices[0][0], devices[0][0], f"{TAG} radio check"))
        socks[0].sendall(emergency(devices[0][0], devices[0][0], 45.4215, -75.6972))
    time.sleep(2.5)

    code, devs = a.call("/api/devices")
    mine = [d for d in (devs or []) if str(d.get("uid", "")).startswith(TAG)]
    ok(f"{len(mine)} test device(s) visible on the server") if len(mine) >= 2 \
        else bad(f"expected 2 test devices, found {len(mine)}")

    code, hist = a.call("/api/history?minutes=60")
    n = len([h for h in (hist or []) if str(h.get("uid", "")).startswith(TAG)])
    ok(f"{n} tracking breadcrumbs stored") if n >= 4 else bad(f"only {n} breadcrumbs stored")

    if mine:
        uid0 = mine[0]["uid"]
        code, track = a.call(f"/api/devices/{urllib.parse.quote(uid0)}/track")
        ok("per-device track history readable") if code == 200 \
            else bad(f"/api/devices/<uid>/track returned {code}")

    code, msgs = a.call("/api/chat")
    hit = [m for m in (msgs or []) if TAG in str(m.get("message", ""))]
    ok(f"chat stored and readable ({len(hit)} test message(s))") if hit \
        else bad("chat message not stored")

    code, alerts = a.call("/api/alerts")
    mine_alert = [x for x in (alerts or []) if TAG in str(x.get("callsign", ""))]
    ok(f"911 emergency active ({mine_alert[0].get('callsign')})") if mine_alert \
        else bad("emergency alert not recorded")

    code, stats = a.call("/api/stats")
    ok(f"stats endpoint OK ({stats})") if code == 200 else bad(f"/api/stats returned {code}")

    t.join(timeout=6)
    ok(f"live event stream delivered {len(events)} event(s)") if events \
        else bad("SSE /api/stream delivered no events")

    # ---------------------------------------------------------------------
    head("7. Cameras and the stream registry")
    cam = f"{TAG}-cam"
    code, r = a.call("/api/streams",
                     {"name": cam, "source": "rtsp://127.0.0.1:8554/nonexistent",
                      "lat": 45.42, "lon": -75.69}, method="POST")
    if code == 201:
        created["paths"].append(r.get("path", cam))
        ok(f"camera registered as path '{r.get('path')}'")
    else:
        bad(f"camera registration failed: {code} {r}")

    code, streams = a.call("/api/streams")
    ok(f"stream list readable ({len(streams)} stream(s))") if code == 200 \
        else bad(f"/api/streams returned {code}")

    code, r = a.call("/api/streams/record", {"path": created["paths"][0] if created["paths"] else cam,
                                             "enabled": True}, method="POST")
    ok("recording can be switched on") if code == 200 and r.get("recording") \
        else bad(f"enabling recording returned {code} {r}")
    code, r = a.call("/api/streams/record", {"path": created["paths"][0] if created["paths"] else cam,
                                             "enabled": False}, method="POST")
    ok("recording can be switched off") if code == 200 and not r.get("recording") \
        else bad(f"disabling recording returned {code} {r}")

    # ---------------------------------------------------------------------
    head("8. Video ingest - RTSP, RTMP and SRT")
    live = f"{TAG}-live"
    ptok = args.publish_token

    def ff_publish(url, extra):
        cmd = ([ffmpeg, "-loglevel", "error", "-re",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-g", "15", "-pix_fmt", "yuv420p"] + extra + [url])
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def ffmpeg_has(proto):
        try:
            out = subprocess.run([ffmpeg, "-hide_banner", "-protocols"],
                                 capture_output=True, timeout=20).stdout.decode()
            return any(line.strip() == proto for line in out.split())
        except Exception:  # noqa: BLE001
            return False

    def readable(path, timeout=20):
        """Read the path back over RTSP and report the codec ffprobe sees."""
        r = subprocess.run(
            [ffprobe, "-v", "error", "-rtsp_transport", "tcp",
             "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height",
             "-of", "json", f"rtsp://{mhost}:8554/{path}"],
            capture_output=True, timeout=timeout)
        if r.returncode != 0:
            return None
        try:
            st = json.loads(r.stdout.decode())["streams"][0]
            return f"{st['codec_name']} {st['width']}x{st['height']}"
        except Exception:  # noqa: BLE001
            return None

    if args.no_video or not ffmpeg or not ffprobe:
        skip("video ingest - ffmpeg/ffprobe not available here")
        note("run this script from a machine that has ffmpeg, with --media-host <server>")
    else:
        for proto, url, extra in (
                ("RTSP", f"rtsp://{mhost}:8554/{live}-rtsp?token={ptok}",
                 ["-f", "rtsp", "-rtsp_transport", "tcp"]),
                ("RTMP", f"rtmp://{mhost}:1935/{live}-rtmp?token={ptok}", ["-f", "flv"]),
                ("SRT", f"srt://{mhost}:8890?streamid=publish:{live}-srt::{ptok}",
                 ["-f", "mpegts"])):
            if proto == "SRT" and not ffmpeg_has("srt"):
                skip("SRT ingest - this ffmpeg was built without SRT support")
                note("server side is MediaMTX srt: yes on udp/8890; test from a host "
                     "whose ffmpeg lists srt in 'ffmpeg -protocols'")
                continue
            p = ff_publish(url, extra)
            procs.append(p)
            path = f"{live}-{proto.lower()}"
            created["paths"].append(path)
            time.sleep(6)
            if p.poll() is not None:
                bad(f"{proto} publish failed: {p.stderr.read().decode()[:160].strip()}")
                continue
            desc = readable(path)
            ok(f"{proto} ingest works, plays back as {desc}") if desc \
                else bad(f"{proto} published but the stream could not be read back")

        head("9. Publish authorization")
        if not ptok:
            skip("no PUBLISH_TOKEN configured - publish is open on this server")
        else:
            p = ff_publish(f"rtsp://{mhost}:8554/{TAG}-noauth",
                           ["-f", "rtsp", "-rtsp_transport", "tcp", "-t", "3"])
            time.sleep(4)
            rc = p.poll()
            if rc is None:
                p.kill()
                bad("publishing WITHOUT the token was accepted")
            else:
                ok("publishing without the token is refused")

    # ---------------------------------------------------------------------
    head("10. WebRTC playback tickets")
    wpath = f"{live}-rtsp" if (ffmpeg and not args.no_video) else (created["paths"] or [""])[0]
    offer = ("v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n"
             "m=video 9 UDP/TLS/RTP/SAVPF 96\r\nc=IN IP4 0.0.0.0\r\n"
             "a=recvonly\r\na=rtpmap:96 H264/90000\r\n")

    def whep(path, token=None):
        url = f"http://{mhost}:8889/{path}/whep" + (f"?token={token}" if token else "")
        req = urllib.request.Request(url, data=offer.encode(),
                                     headers={"Content-Type": "application/sdp"},
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:  # noqa: BLE001
            return 0

    code, ticket = a.call(f"/api/streams/ticket?path={urllib.parse.quote(wpath)}")
    tkt = (ticket or {}).get("token") if isinstance(ticket, dict) else None
    ok("playback ticket minted for a logged-in session") if tkt \
        else bad(f"ticket endpoint returned {code} {ticket}")

    st = whep(wpath)
    ok(f"WebRTC playback without a ticket is refused (HTTP {st})") if st in (401, 403) \
        else bad(f"WebRTC without a ticket returned {st}, expected 401/403")

    if tkt:
        st = whep(wpath, tkt)
        ok(f"WebRTC playback with a valid ticket passes authorization (HTTP {st})") \
            if st not in (401, 403, 0) \
            else bad(f"WebRTC with a valid ticket returned {st}")

    # ---------------------------------------------------------------------
    head("11. Recording and playback")
    if args.no_video or not ffmpeg:
        skip("recording - needs ffmpeg to produce a stream to record")
    else:
        rpath = f"{TAG}-rec"
        created["paths"].append(rpath)
        code, r = a.call("/api/streams/record", {"path": rpath, "enabled": True}, method="POST")
        if code != 200:
            bad(f"could not enable recording: {code} {r}")
        else:
            ok("recording enabled for the test path")
            p = ff_publish(f"rtsp://{mhost}:8554/{rpath}?token={ptok}",
                           ["-f", "rtsp", "-rtsp_transport", "tcp", "-t", "12"])
            procs.append(p)
            time.sleep(15)
            p.wait(timeout=10)
            time.sleep(2)
            code, recs = a.call(f"/api/recordings?path={urllib.parse.quote(rpath)}")
            if code == 200 and recs:
                ok(f"{len(recs)} recorded segment(s) listed via /api/recordings")
                seg = recs[0]
                note(f"segment start={seg.get('start')} duration={seg.get('duration')}")
                try:
                    url = (f"http://{mhost}:9996/get?path={urllib.parse.quote(rpath)}"
                           f"&start={urllib.parse.quote(str(seg.get('start')))}&duration=3&format=mp4")
                    with urllib.request.urlopen(url, timeout=20) as rr:
                        blob = rr.read(65536)
                    ok(f"playback API returned {len(blob)} bytes of recorded video") if blob \
                        else bad("playback API returned an empty body")
                except Exception as e:  # noqa: BLE001
                    bad(f"playback fetch failed: {e}")
            else:
                bad(f"no recordings listed for {rpath} ({code} {recs})")
            a.call("/api/streams/record", {"path": rpath, "enabled": False}, method="POST")

    # ---------------------------------------------------------------------
    head("12. Certificates and the ATAK truststore")
    if not local:
        skip("certificate files - run on the server itself to check them")
    else:
        cert = env.get("CERT_FILE", "/etc/rtak/certs/server.pem")
        if os.path.exists(cert):
            r = subprocess.run([openssl, "x509", "-in", cert, "-noout",
                                "-ext", "subjectAltName"], capture_output=True)
            sans = r.stdout.decode().strip().splitlines()[-1].strip() if r.returncode == 0 else ""
            host = env.get("SERVER_HOST", "")
            ok(f"server certificate covers {host}") if host and host in sans \
                else bad(f"server certificate does not cover SERVER_HOST={host}: {sans}")
            note(f"SANs: {sans}")
        else:
            bad(f"no server certificate at {cert}")
        ok("certificate authority present") if os.path.exists(env.get("CA_FILE", "")) \
            else bad("CA certificate missing")

        ts = "/etc/rtak/certs/truststore.p12"
        if os.path.exists(ts):
            r = subprocess.run([openssl, "pkcs12", "-info", "-in", ts, "-nokeys",
                                "-passin", "pass:atakatak", "-legacy"], capture_output=True)
            ok("truststore.p12 readable in the ATAK-compatible format") if r.returncode == 0 \
                else bad(f"truststore unreadable: {r.stderr.decode()[:160]}")
        else:
            bad("truststore.p12 was not generated")

    # ---------------------------------------------------------------------
    head("13. Cleanup")
    if args.keep:
        skip("cleanup skipped (--keep): "
             f"users={created['users']} devices={created['devices']} paths={created['paths']}")
    else:
        for s in socks:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        socks.clear()
        for p in procs:
            if p.poll() is None:
                p.kill()
        time.sleep(1.0)
        for uid in created["devices"]:
            a.call(f"/api/devices?uid={urllib.parse.quote(uid)}", method="DELETE")
        for name in created["users"]:
            a.call(f"/api/users?username={urllib.parse.quote(name)}", method="DELETE")
        for path in dict.fromkeys(created["paths"]):
            a.call(f"/api/streams?path={urllib.parse.quote(path)}", method="DELETE")
        a.call(f"/api/enroll/tokens?username={urllib.parse.quote(enroll_user)}",
               method="DELETE")

        code, devs = a.call("/api/devices")
        left_d = [d for d in (devs or []) if str(d.get("uid", "")).startswith(TAG)]
        code, users = a.call("/api/users")
        left_u = [u for u in (users or []) if str(u.get("username", "")).startswith(TAG)]
        ok("test devices removed") if not left_d else bad(f"test devices left behind: {left_d}")
        ok("test users removed") if not left_u else bad(f"test users left behind: {left_u}")

        if local:
            import glob
            for path in dict.fromkeys(created["paths"]):
                for d in glob.glob(f"/var/lib/rtak/recordings/{path}"):
                    shutil.rmtree(d, ignore_errors=True)
            ok("test recordings deleted from disk")
        else:
            note("recordings for the test paths live under /var/lib/rtak/recordings on the server")

    shutil.rmtree(work, ignore_errors=True)
    return report()


def report(work=None, procs=None, socks=None):
    for p in (procs or []):
        if p.poll() is None:
            p.kill()
    for s in (socks or []):
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
    if work:
        shutil.rmtree(work, ignore_errors=True)
    print("\n" + "=" * 60)
    print(f"  PASSED: {len(PASS)}    FAILED: {len(FAIL)}    SKIPPED: {len(SKIP)}")
    print("=" * 60)
    for f in FAIL:
        print(f"  {C_R}-{C_0} {f}")
    for s in SKIP:
        print(f"  {C_Y}~{C_0} {s}")
    if not FAIL:
        print(f"  {C_G}Every checked subsystem works on this server.{C_0}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
