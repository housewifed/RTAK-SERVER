# Installing RTAK Server on Linux

Written for someone who is **not** a Linux specialist. If you can install an
operating system from a USB stick and type one command, you can do this.

---

## 1. Choose and install the operating system

**Use Ubuntu Server 24.04 LTS.** It has the longest support window (2029, free),
the widest hardware support, and the most help available online.

1. Download the ISO: <https://ubuntu.com/download/server>
2. Write it to a USB stick with [balenaEtcher](https://etcher.balena.io) or
   [Rufus](https://rufus.ie).
3. Boot the target computer from the USB stick and follow the installer:
   - Accept the defaults for language/keyboard.
   - **Network:** let it use DHCP for now (we fix the address in step 2).
   - **Storage:** "Use an entire disk" is fine.
   - **Profile:** pick a username and password you will remember.
   - **Tick "Install OpenSSH server"** — this lets you manage the box remotely.
   - Skip all the "featured server snaps".
4. Reboot and log in.

> Debian 12 works identically if you prefer it. Avoid Fedora/Arch (they change
> too fast for an appliance) and Alpine (different C library — the installer
> will not run).

## 2. Give the machine a fixed address

The server must keep the same IP or devices will lose it. Easiest way: in your
**router**, find the DHCP / LAN settings and add a **DHCP reservation** for this
machine's MAC address.

Find the current address and MAC:

```bash
ip -4 addr show scope global | grep inet
ip link | grep ether
```

## 3. Install RTAK Server

Copy the installer to the machine (USB stick, or `scp` from your laptop), then:

```bash
chmod +x rtak-server-1.3.0-linux-amd64.run
sudo ./rtak-server-1.3.0-linux-amd64.run
```

Use the **amd64** file for a normal PC, **arm64** for a Raspberry Pi 5 or ARM VM.
(`uname -m` prints `x86_64` for amd64, `aarch64` for arm64.)

It asks:

1. **The address devices will use** — the machine's LAN IP for a local-only
   setup, or a DDNS name like `rtak.example.com` if phones connect from outside.
   This goes into the TLS certificate, so get it right (you can change it later
   with `sudo rtak renew-certs`).
2. **A domain for HTTPS** — leave blank unless you have a public name pointing
   at this machine (see §6).

When it finishes it prints your **admin password**. Save it — it is shown once.

**Nothing else needs installing.** Python, OpenSSL, the video engine and the web
server are all inside the installer.

## 4. First login

Open `http://<the-address-it-printed>:8080` from a computer on the same network
and log in as `admin`.

Check everything is healthy:

```bash
rtak doctor
```

## 5. Add a phone

```bash
sudo rtak enroll ALPHA-1
```

Then in the web UI use **Enroll device** and scan the QR code from ATAK/iTAK.
Devices connect to port **8089**; enrollment uses **8446**.

## 6. Optional: a clean HTTPS address

Without this you use `http://<ip>:8080`, which is fine on a trusted LAN but sends
the admin login unencrypted. With it you get `https://rtak.example.com` with a
real, auto-renewing certificate.

You need: a public DNS name (a DDNS hostname is fine) pointing at your public IP,
and ports **80** and **443** forwarded to this machine.

```bash
sudo nano /etc/rtak/rtak.env      # set TAK_DOMAIN=rtak.example.com
sudo systemctl enable --now rtak-caddy
sudo rtak restart
```

Caddy fetches the Let's Encrypt certificate within a few seconds. Video keeps
working over the same address.

## 7. Firewall

If `ufw` is active the installer opens the right ports automatically. To do it
by hand:

```bash
sudo ufw allow 8089/tcp    # devices (always needed)
sudo ufw allow 8446/tcp    # enrollment
sudo ufw allow 8080/tcp    # web UI
sudo ufw allow 8554/tcp; sudo ufw allow 1935/tcp
sudo ufw allow 8889/tcp; sudo ufw allow 9996/tcp
sudo ufw allow 8890/udp; sudo ufw allow 8189/udp
# only with HTTPS:
sudo ufw allow 80/tcp; sudo ufw allow 443/tcp
```

Only **8089** is needed for devices that are already enrolled.

## 8. Backups

```bash
sudo rtak backup                      # -> /var/backups/rtak-<date>.tar.gz
sudo rtak restore /var/backups/rtak-2026-09-07.tar.gz
```

This saves settings, the certificate authority and the device database — the
things that are painful to recreate. Recordings are excluded (they are large).
Copy the file somewhere off the machine.

## 9. Troubleshooting

Run this first — it checks services, ports, certificates, database and disk:

```bash
rtak doctor
```

| Symptom | Likely cause / fix |
|---|---|
| Web UI won't load | `rtak status`; then `rtak logs takcore` |
| Phone says "registration failed" | The certificate must cover the address the phone dials. `rtak doctor` prints what it covers; fix with `sudo rtak renew-certs` |
| Phone connects on Wi-Fi but not mobile data | Port **8089** (and 8446 to enroll) isn't forwarded on the router |
| Video won't play remotely | Forward **8189/udp**; check `SERVER_HOST` is the public name |
| Disk filling up | Recordings. `du -sh /var/lib/rtak/recordings`; turn recording off in the UI |
| `Address already in use` in `rtak logs` | Another program already owns the port. See "Sharing the machine" below |
| Everything is broken | `sudo rtak restart`, then `rtak doctor` |

Logs:

```bash
rtak logs -f              # everything
rtak logs takcore         # just the TAK core
```

### Sharing the machine with other services

Port 8080 is a popular default (SABnzbd, Home Assistant, Jenkins), and :80 is
usually taken the moment anything web-facing is installed. RTAK does not fight
for either - it just needs to be told which ports are free.

The installer refuses to start on an occupied web port and tells you so. Pick
another one at install time:

```bash
sudo ./rtak-server-1.3.0-linux-amd64.run --yes --http-port 8081
```

or afterwards, in `/etc/rtak/rtak.env`:

```bash
HTTP_PORT=8081          # web UI / API - the CLI, Caddy and MediaMTX follow it
CADDY_HTTP_PORT=8880    # Caddy's plain-HTTP listener, if :80 belongs elsewhere
```

then `sudo rtak restart`. Moving `CADDY_HTTP_PORT` off 80 keeps HTTPS on 443;
Let's Encrypt then validates over TLS-ALPN on 443, so only **443** has to be
forwarded in the router. Devices are unaffected - they use 8089 and 8446.

## 10. What got installed where

| Path | Contents |
|---|---|
| `/opt/rtak` | the program (safe to delete on uninstall) |
| `/etc/rtak/rtak.env` | all your settings — the only file you edit |
| `/etc/rtak/certs` | certificate authority + server certificate |
| `/var/lib/rtak` | device database and recordings |

Services: `rtak-takcore`, `rtak-mediamtx`, and `rtak-caddy` (if HTTPS enabled).
They run as the unprivileged `rtak` user and start automatically at boot.
