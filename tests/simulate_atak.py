"""Simulated ATAK clients for testing and load testing.

Each simulated device connects over TCP, sends a position CoT every
--interval seconds while random-walking around --center, and reads (and
discards) whatever the server broadcasts back.

Examples:
    python3 server/tests/simulate_atak.py --devices 3
    python3 server/tests/simulate_atak.py --devices 150 --interval 5 --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import re as _re
import ssl
import time
from datetime import datetime, timedelta, timezone

def _make_ssl_context(args) -> "ssl.SSLContext | None":
    if not getattr(args, "tls", False):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if getattr(args, "cafile", None):
        ctx.load_verify_locations(args.cafile)
        ctx.check_hostname = False  # certs are IP-SAN lab certs
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    if getattr(args, "cert", None) and getattr(args, "key", None):
        ctx.load_cert_chain(args.cert, args.key)
    return ctx


_TIME_RE = _re.compile(rb'<event [^>]*?time="([0-9T:.\-]+)Z"')


def _extract_event_times(chunk: bytes) -> "list[float]":
    out = []
    for m in _TIME_RE.finditer(chunk):
        try:
            dt = datetime.strptime(m.group(1).decode(),
                                   "%Y-%m-%dT%H:%M:%S.%f")
            out.append(dt.replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return out


TEAMS = ["Cyan", "Red", "Green", "Yellow", "Blue", "Orange", "Purple", "Magenta"]
ROLES = ["Team Member", "Team Lead", "Medic", "Sniper", "RTO", "K9"]


def cot_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def sa_event(uid: str, callsign: str, team: str, role: str,
             lat: float, lon: float, course: float, speed: float,
             video_url: str = "") -> bytes:
    now = datetime.now(timezone.utc)
    stale = now + timedelta(seconds=75)
    video = f'<__video url="{video_url}"/>' if video_url else ""
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<event version="2.0" uid="{uid}" type="a-f-G-U-C" how="m-g" '
        f'time="{cot_time(now)}" start="{cot_time(now)}" stale="{cot_time(stale)}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="120.0" ce="5.0" le="9999999.0"/>'
        f"<detail>"
        f'<takv os="34" version="5.2.0" device="SIM DEVICE" platform="ATAK-SIM"/>'
        f'<contact callsign="{callsign}" endpoint="*:-1:stcp"/>'
        f'<uid Droid="{callsign}"/>'
        f'<__group role="{role}" name="{team}"/>'
        f'<status battery="{random.randint(35, 100)}"/>'
        f'<track course="{course:.1f}" speed="{speed:.1f}"/>'
        f"{video}"
        f"</detail></event>"
    ).encode()


def chat_event(uid: str, callsign: str, message: str) -> bytes:
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=5)
    m = (message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return (
        f'<event version="2.0" uid="GeoChat.{uid}.All.{random.randint(1,1<<30)}" '
        f'type="b-t-f" how="h-g-i-g-o" time="{cot_time(now)}" '
        f'start="{cot_time(now)}" stale="{cot_time(stale)}">'
        f'<point lat="0" lon="0" hae="0" ce="9999999" le="9999999"/>'
        f'<detail><__chat chatroom="All Chat Rooms" id="All Chat Rooms" '
        f'senderCallsign="{callsign}"><chatgrp uid0="{uid}" uid1="All Chat Rooms" '
        f'id="All Chat Rooms"/></__chat>'
        f'<remarks source="BAO.F.ATAK.{uid}">{m}</remarks></detail></event>'
    ).encode()


def emergency_event(uid: str, callsign: str, lat: float, lon: float) -> bytes:
    now = datetime.now(timezone.utc)
    stale = now + timedelta(minutes=10)
    return (
        f'<event version="2.0" uid="{uid}-9-1-1" type="b-a-o-tbl" how="m-g" '
        f'time="{cot_time(now)}" start="{cot_time(now)}" stale="{cot_time(stale)}">'
        f'<point lat="{lat:.6f}" lon="{lon:.6f}" hae="10" ce="5" le="9999999"/>'
        f'<detail><emergency type="911 Alert">{callsign}</emergency>'
        f'<contact callsign="{callsign}"/></detail></event>'
    ).encode()


async def run_monitor(args, stats: dict) -> None:
    """Read-only client: measures now - event.time for every broadcast."""
    try:
        reader, writer = await asyncio.open_connection(
            args.host, args.port, ssl=_make_ssl_context(args))
    except OSError as e:
        print(f"monitor: connect failed: {e}")
        stats["errors"] += 1
        return
    try:
        deadline = time.time() + args.duration
        while time.time() < deadline:
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            if not chunk:
                break
            now = time.time()
            for t in _extract_event_times(chunk):
                if 0 <= now - t < 60:  # discard clock-skew nonsense
                    stats["latencies"].append(now - t)
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def run_device(i: int, args, stats: dict) -> None:
    uid = f"SIM-{i:04d}"
    callsign = f"SIM-{i:04d}"
    team = TEAMS[i % len(TEAMS)]
    role = ROLES[i % len(ROLES)]
    # scatter start positions ~2 km around center
    lat = args.lat + random.uniform(-0.02, 0.02)
    lon = args.lon + random.uniform(-0.02, 0.02)
    heading = random.uniform(0, 360)

    try:
        ssl_ctx = _make_ssl_context(args)
        reader, writer = await asyncio.open_connection(
            args.host, args.port, ssl=ssl_ctx)
    except OSError as e:
        print(f"{uid}: connect failed: {e}")
        stats["errors"] += 1
        return
    stats["connected"] += 1

    async def drain_reads() -> None:
        try:
            while await reader.read(65536):
                stats["rx_chunks"] += 1
        except Exception:
            pass

    reader_task = asyncio.create_task(drain_reads())
    deadline = time.time() + args.duration
    video_url = ""
    if args.video and i <= args.video:
        video_url = f"rtsp://{args.host}:8554/live/{uid}"
    ticks = 0
    try:
        while time.time() < deadline:
            heading += random.uniform(-25, 25)
            speed = random.uniform(0.5, 2.5)  # m/s
            dist = speed * args.interval / 111_320  # deg latitude
            lat += dist * math.cos(math.radians(heading))
            lon += dist * math.sin(math.radians(heading)) / max(
                math.cos(math.radians(lat)), 0.1)
            writer.write(sa_event(uid, callsign, team, role, lat, lon,
                                  heading % 360, speed, video_url))
            # device 1 sends a chat message every ~5 ticks
            if args.chat and i == 1 and ticks % 5 == 2:
                writer.write(chat_event(uid, callsign,
                                        f"{callsign} radio check {ticks}"))
            # device 1 raises an emergency once, at tick 3, if --emergency
            if args.emergency and i == 1 and ticks == 3:
                writer.write(emergency_event(uid, callsign, lat, lon))
            await writer.drain()
            stats["sent"] += 1
            ticks += 1
            await asyncio.sleep(args.interval * random.uniform(0.9, 1.1))
    except Exception as e:  # noqa: BLE001
        print(f"{uid}: {e}")
        stats["errors"] += 1
    finally:
        reader_task.cancel()
        writer.close()


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8087)
    ap.add_argument("--devices", type=int, default=3)
    ap.add_argument("--interval", type=float, default=2.0,
                    help="seconds between position reports")
    ap.add_argument("--duration", type=float, default=30.0,
                    help="seconds to run before disconnecting")
    ap.add_argument("--lat", type=float, default=38.8977)
    ap.add_argument("--lon", type=float, default=-77.0365)
    ap.add_argument("--video", type=int, default=0, metavar="N",
                    help="first N devices announce a __video stream URL")
    ap.add_argument("--chat", action="store_true",
                    help="device 1 periodically sends GeoChat messages")
    ap.add_argument("--emergency", action="store_true",
                    help="device 1 raises a 911 emergency beacon once")
    ap.add_argument("--tls", action="store_true",
                    help="connect with TLS (use with --port 8089)")
    ap.add_argument("--cafile", help="CA to verify the server (optional)")
    ap.add_argument("--cert", help="client certificate PEM (mTLS)")
    ap.add_argument("--key", help="client key PEM (mTLS)")
    ap.add_argument("--monitor", action="store_true",
                    help="add a read-only client that reports broadcast latency")
    args = ap.parse_args()

    stats = {"connected": 0, "sent": 0, "rx_chunks": 0, "errors": 0, "latencies": []}
    t0 = time.time()
    tasks = [run_device(i + 1, args, stats) for i in range(args.devices)]
    if args.monitor:
        tasks.append(run_monitor(args, stats))
    await asyncio.gather(*tasks)
    dt = time.time() - t0
    print(f"done in {dt:.1f}s: {stats['connected']}/{args.devices} connected, "
          f"{stats['sent']} events sent, {stats['rx_chunks']} broadcast chunks "
          f"received, {stats['errors']} errors")
    if stats.get("latencies"):
        lat = sorted(stats["latencies"])
        p50 = lat[len(lat) // 2]
        p95 = lat[int(len(lat) * 0.95)]
        print(f"broadcast latency: p50={p50*1000:.0f}ms p95={p95*1000:.0f}ms "
              f"({len(lat)} samples)")


if __name__ == "__main__":
    asyncio.run(main())
