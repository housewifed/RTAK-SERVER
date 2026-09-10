# RTAK Server

A TAK-compatible server (ATAK / iTAK) that installs on a Linux computer with
**one command** and needs **nothing pre-installed** — no Docker, no Python, no
OpenSSL. Everything it needs is inside the installer.

It is the same server as the Docker build, packaged to run natively:

- **Live map** with device tracking, breadcrumbs and playback
- **Follow a device**: the map stays centred on it as it moves, and its popup
  (live video included) travels with it
- **Record tracks** for chosen devices (or all of them), then replay a recording
  or any custom time range — no camera required
- **Street or satellite** basemap, switchable on the map (satellite imagery
  carries a place-name overlay so you can still say where something is)
- **Chat** and **911 emergency** alerts
- **Certificate enrollment** for ATAK/iTAK (Marti API) over mTLS
- **Video**: RTSP / RTMP / SRT ingest, WebRTC playback, recording
- **Optional HTTPS** with an automatic free Let's Encrypt certificate

---

## Install

Grab the installer for your CPU from the
[latest release](https://github.com/housewifed/RTAK-SERVER/releases/latest)
and run it:

```bash
# normal PC / server (uname -m says x86_64)
wget https://github.com/housewifed/RTAK-SERVER/releases/download/v1.2.0/rtak-server-1.2.0-linux-amd64.run
chmod +x rtak-server-1.2.0-linux-amd64.run
sudo ./rtak-server-1.2.0-linux-amd64.run
```

On a Raspberry Pi 5 / ARM machine (`uname -m` says `aarch64`) use the
`linux-arm64.run` file instead.

That's it. It asks two questions (the address devices will use, and whether you
want HTTPS), then installs, generates certificates, starts the services, enables
them at boot, and prints your admin password.

Non-interactive:

```bash
sudo ./rtak-server-1.2.0-linux-amd64.run --yes --host rtak.example.com
```

**Already running something on 8080 or 80?** SABnzbd, Home Assistant, Nextcloud
and Jenkins all like those ports. Tell the installer which ones are free instead
— nothing else has to move:

```bash
sudo ./rtak-server-1.2.0-linux-amd64.run --yes --host rtak.example.com \
     --http-port 8081 --caddy-http-port 8880
```

`--caddy-http-port` moves only Caddy's plain-HTTP listener. HTTPS stays on 443,
Let's Encrypt validates over TLS-ALPN, and just **443** needs forwarding. The
installer refuses to start on a port another program owns, and names it.

**Recommended OS: Ubuntu Server 24.04 LTS.** Debian 12 also works. See
[docs/INSTALL.md](docs/INSTALL.md) for the full guide.

## Everyday use

```bash
rtak status      # is everything running?
rtak doctor      # full self-test - run this first if something looks wrong
rtak enroll      # create a token to add a phone
rtak logs -f     # watch what's happening
rtak backup      # save database + certificates
rtak restart
```

## Ports

| Port | Proto | What |
|------|-------|------|
| 8080 | TCP | web UI / API (`HTTP_PORT`, moveable) |
| 8089 | TCP | device CoT over TLS (mTLS) — **the one devices always need** |
| 8446 | TCP | certificate enrollment |
| 8554 / 1935 | TCP | RTSP / RTMP video in |
| 8890 / 8189 | UDP | SRT in / WebRTC media |
| 8889 / 9996 | TCP | WebRTC playback / recording playback |
| 80 + 443 | TCP | only if you enable HTTPS (80 is `CADDY_HTTP_PORT`, moveable) |

## Testing it works

`rtak doctor` is the 30-second health check. For a full end-to-end exercise of
every subsystem — enrollment, devices over mTLS, chat, 911, users and roles,
cameras, RTSP/RTMP/SRT ingest, WebRTC tickets, recording and playback — see
**[docs/TESTING.md](docs/TESTING.md)**:

```bash
python3 tests/full_system_check.py --api http://<server>:8080 \
    --media-host <server> --password '<admin password>'
```

It cleans up everything it creates and never touches data it did not make.

## Upgrading

Run the new installer over the top. Your devices, certificates, recordings and
settings are preserved:

```bash
sudo ./rtak-server-1.2.0-linux-amd64.run --yes
```

## Uninstall

```bash
sudo rtak uninstall            # keeps your data
sudo rtak uninstall --purge    # removes everything
```

## Building the installer yourself

Needs network + Docker (Docker is only used to lift the OpenSSL CLI out of a
Debian image; the resulting installer is fully offline):

```bash
./packaging/build/build-bundle.sh          # both architectures -> dist/
```

## Layout

```
app/            the server (takcore, pure Python stdlib) + web UI
packaging/
  linux/        install.sh, uninstall.sh, rtak CLI, systemd units, config
  build/        build-bundle.sh - assembles the self-contained installer
tests/          unit tests + an end-to-end check for an installed server
docs/           DESIGN.md (why it is built this way), INSTALL.md (full guide)
```
