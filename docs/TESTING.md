# Testing RTAK Server

Three levels, cheapest first:

| Level | Command | Time | What it proves |
|---|---|---|---|
| Health check | `rtak doctor` | ~5s | services, ports, certificates, database, disk |
| Full system check | `tests/full_system_check.py` | ~90s | every subsystem end to end, against a live server |
| Install check | container run, below | ~5min | a fresh install, an upgrade, and port-conflict handling |

Plus the unit tests (CoT parsing, auth, tokens, retention, Marti conformance),
which need no server and no dependencies — 110 tests, about 10 seconds:

```bash
PYTHONPATH=app python3 -m unittest discover -s tests -p "test_*.py"
```

(`python3 -m pytest tests` works too, if you have pytest.)

---

## 1. Health check

```bash
rtak status      # services + listening ports
rtak doctor      # adds certificates, database integrity, disk, bundled tools
```

`rtak doctor` exits non-zero if anything failed, so it works in a cron job or a
monitoring probe.

## 2. Full system check

`tests/full_system_check.py` drives the real paths a phone and a browser use.
Standard library only — the bundled interpreter runs it — plus **ffmpeg** for
the video sections.

### Running it

**On the server.** Reads `/etc/rtak/rtak.env` for the port, the admin password
and the publish token, so it needs no arguments:

```bash
sudo -u rtak /opt/rtak/runtime/python/bin/python3 tests/full_system_check.py
```

Most servers have no ffmpeg, so the video sections report SKIP.

**From a machine with ffmpeg** (a laptop on the same network) — this is the run
that covers video:

```bash
python3 tests/full_system_check.py \
    --api http://192.168.2.101:8081 \
    --media-host 192.168.2.101 \
    --user admin --password '<admin password>' \
    --publish-token '<PUBLISH_TOKEN from rtak.env>'
```

Run it both ways for complete coverage: the remote run exercises video, the
local run exercises the certificate files on disk.

| Option | Meaning |
|---|---|
| `--api URL` | base URL of the web API (default `http://127.0.0.1:$HTTP_PORT`) |
| `--media-host HOST` | host for RTSP/RTMP/SRT/WebRTC (default: the API host) |
| `--user`, `--password` | admin credentials (default: from `rtak.env`) |
| `--publish-token` | value of `PUBLISH_TOKEN` (default: from `rtak.env`) |
| `--no-video` | skip the ffmpeg sections |
| `--keep` | leave the test users, devices and camera paths behind |

### What it checks

| # | Section | Checks |
|---|---|---|
| 1 | Reachability | web API answers; CoT, enrollment, RTSP, RTMP, WebRTC ports open |
| 2 | Authentication | anonymous API refused (401), wrong password refused, admin login, session identity |
| 3 | Users and roles | create viewer + operator, viewer denied writes (403), operator allowed chat but denied user creation, last admin cannot be demoted (409), role change applies |
| 4 | Enrollment | token issued, CSR signed by the Marti API, certificate verifies against the CA, `tls/config` + `version/config` + `clientEndPoints` answer, `enroll.zip` refused without a token and served with one, `mode=enroll` package, `ca.mobileconfig` for iTAK, and **the address the QR codes encode** (`web_base`) actually serves both files to an unauthenticated client |
| 5 | Devices over mTLS | two devices connect on 8089 with client certificates and report positions; plain TCP to that port is rejected |
| 6 | Tracking and messaging | devices visible, breadcrumbs stored, per-device track, chat stored and a message deleted through `DELETE /api/chat?id=` (operator denied, unknown id 404, no args 400), 911 alert raised **and cleared** through `DELETE /api/alerts` (viewer denied, unknown uid 404), stats, live SSE event stream |
| 7 | Cameras | register a camera path, list streams, recording on and off |
| 8 | Video ingest | publish over **RTSP**, **RTMP** and **SRT**, each read back with ffprobe to confirm the codec and resolution |
| 9 | Publish authorization | publishing without `PUBLISH_TOKEN` is refused |
| 10 | WebRTC playback | ticket minted for a logged-in session; WHEP without a ticket refused (401); with a ticket, authorization passes |
| 11 | Recording and playback | enable recording, publish 12s, segment appears in `/api/recordings`, playback API returns video bytes |
| 12 | Certificates | server certificate covers `SERVER_HOST`, CA present, `truststore.p12` readable in the ATAK (legacy PBE) format |
| 13 | Cleanup | removes every user, device, camera path and recording it created |

### Expected output

```
============================================================
  PASSED: 58    FAILED: 0    SKIPPED: 2
============================================================
```

SKIPs are environmental, not failures — they say which machine to run from.

### SRT needs an ffmpeg built with SRT

Many ffmpeg builds (including Homebrew's) ship without the SRT protocol, and
the script skips that section rather than reporting a false failure. Check with:

```bash
ffmpeg -hide_banner -protocols | tr ' ' '\n' | grep -x srt
```

If it prints nothing, run the SRT test from a container that has it:

```bash
docker run --rm ubuntu:24.04 bash -c '
  apt-get update -qq && apt-get install -y -qq ffmpeg
  ffmpeg -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=15 \
    -c:v libx264 -preset ultrafast -g 15 -pix_fmt yuv420p -t 12 -f mpegts \
    "srt://SERVER:8890?streamid=publish:srttest::PUBLISH_TOKEN" &
  sleep 7
  ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height \
    -of json rtsp://SERVER:8554/srttest'
```

A `h264 320x240` stream in the output means SRT ingest works.

### Safety

The script only ever deletes what it created — every object it makes is named
`fullcheck-*`, including the chat messages, which it removes one id at a time.
It performs no bulk device wipe, never calls `DELETE /api/chat?all=1`, and never
edits the database directly, so it is safe to run against a server with real
devices and real chat history. Use `--keep` to inspect the objects afterwards.

The one thing it cannot remove when run remotely is the recordings it makes:
delete `/var/lib/rtak/recordings/fullcheck-*` on the server, or run the script
there, where it cleans them up itself.

## 3. Install check (container)

Verifies the installer itself on a throwaway Ubuntu + systemd machine — a fresh
install, an upgrade over the top, and the port-conflict path.

```bash
./packaging/build/build-bundle.sh arm64        # or amd64, matching your host
docker build -t rtak-systemd -f tests/Dockerfile.systemd tests/
docker run -d --name rtaktest --privileged --cgroupns=host \
    --tmpfs /run --tmpfs /run/lock -v /sys/fs/cgroup:/sys/fs/cgroup:rw rtak-systemd
docker cp dist/rtak-server-*-linux-arm64.run rtaktest:/root/rtak.run

# fresh install
docker exec rtaktest bash -c 'chmod +x /root/rtak.run && /root/rtak.run --yes --host 10.9.9.9'
docker exec rtaktest rtak doctor

# port conflict: occupy 8080, then install again - must refuse, not crash-loop
docker exec rtaktest bash -c '
  systemctl stop rtak-takcore
  setsid nohup python3 -m http.server 8080 --bind 127.0.0.1 >/dev/null 2>&1 </dev/null &
  sleep 1; /root/rtak.run --yes --host 10.9.9.9; echo "exit=$?"'

# and the way out of it
docker exec rtaktest /root/rtak.run --yes --host 10.9.9.9 --http-port 8081
docker exec rtaktest rtak status

docker rm -f rtaktest
```

Expected: the conflicting install exits 1 with

```
ERROR: port 8080 is already in use by another program - python3 (pid 557)
```

and `NRestarts` stays at 0 — a crash loop means the check regressed.

## What no script covers

- **The browser UI.** Log in, confirm the map draws, a device moves, chat sends,
  and a camera plays back over WebRTC.
- **A real phone.** Enroll an actual ATAK or iTAK device with a QR code; the
  script proves the API path but not the app's import flow.
- **Internet reachability.** From mobile data, not Wi-Fi: `https://<domain>`
  loads and the phone connects on 8089.
