# RTAK Server — native Linux distribution (design)

**Goal:** ship the RTAK (TAK Revamp) server as a self-contained Linux package that a
non-specialist installs with one command, on a machine that has **nothing** pre-installed —
no Python, no Docker, no openssl. When `install.sh` finishes, the server is running,
enabled at boot, and behaves exactly like the Docker stack it replaces.

---

## 1. Recommended Linux

**Ubuntu Server 24.04 LTS (Noble)** — primary target.

| Why | Detail |
|---|---|
| Support runway | LTS: security updates to 2029 (2034 with free Ubuntu Pro). Install once, forget. |
| Hardware | Best out-of-the-box driver/NIC support of the server distros — matters on random PCs. |
| Ubiquity | The most-documented server Linux; any error message is one search away. |
| Simple installer | The ISO's guided install is genuinely next-next-finish; no partitioning expertise needed. |
| systemd + apt | Same service/init model this package targets. |

**Also supported: Debian 12 (Bookworm)** — leaner and rock-stable; choose it if the box is
old/low-RAM. Everything here works identically (both are systemd + glibc).

**Avoid** for this role: Fedora/Arch (fast-moving, frequent breaking upgrades), CentOS Stream
(rolling-ish), and any "minimal container" distro (Alpine — musl libc, different binaries).

> Practical guidance: install **Ubuntu Server 24.04 LTS**, choose *minimal install*, enable
> OpenSSH, and give the machine a static IP or DHCP reservation. Nothing else.

---

## 2. Dependency analysis (what actually has to ship)

The server was audited rather than assumed:

- **`takcore` is pure Python standard library.** Zero third-party packages
  (`import` audit: asyncio, sqlite3, ssl, http, xml, zipfile, hmac … all stdlib).
- **Needs the `openssl` CLI** at runtime — `takcore/ca.py` shells out to it to run the
  certificate authority (sign client certs, build PKCS#12), and `make-certs.sh` uses it.
- **Needs two static binaries**: `mediamtx` (video) and `caddy` (HTTPS front door).

Verified on bare `ubuntu:24.04` and `debian:12` images: **openssl, python3 and systemd are all
absent**. Real Ubuntu Server installs do ship them, but building for the bare case makes the
"install nothing" promise unconditional. So the bundle vendors all four.

## 3. Bundling strategy

A single arch-specific tarball, assembled by `packaging/build/build-bundle.sh`:

| Component | Source | Why this way |
|---|---|---|
| CPython 3.12 | `astral-sh/python-build-standalone` (`install_only`) | Relocatable, self-contained interpreter — matches the Docker `python:3.12-slim` runtime. |
| `openssl` CLI | extracted from Debian 12 `.deb`s (+ its `libssl`/`libcrypto`) | Portable down to glibc 2.36 → runs on Debian 12 and Ubuntu 22.04+/24.04. Used only if the system has no openssl. |
| `mediamtx` | upstream release tarball | Single static Go binary. |
| `caddy` | upstream release tarball | Single static Go binary. |
| app | this repo | `takcore/` + `web/`. |

Result: `rtak-server-<version>-linux-<arch>.tar.gz`, plus an optional self-extracting
`rtak-server-<version>-linux-<arch>.run`. **Install is fully offline** — no apt, no pip, no network.

## 4. On-disk layout

```
/opt/rtak/            program files (read-only in normal operation)
  app/{takcore,web}   the server + UI
  runtime/python/     vendored CPython
  runtime/openssl/    vendored openssl CLI + libs (fallback)
  bin/{mediamtx,caddy,rtak}
  config/{mediamtx.yml,Caddyfile}
/etc/rtak/
  rtak.env            all settings, mode 600 (the only file an admin edits)
  certs/              CA + server certificates
/var/lib/rtak/
  takcore.db          SQLite database
  recordings/         video recordings
  caddy/              Let's Encrypt cert store
```

Data (`/etc/rtak`, `/var/lib/rtak`) is deliberately separate from the program (`/opt/rtak`) so
re-running the installer upgrades code **without touching devices, certs or recordings**.

## 5. Services

Three systemd units plus a grouping target, running as a dedicated non-login `rtak` user:

- `rtak-takcore.service` — CoT/TLS 8089, web+API 8080, enrollment 8446
- `rtak-mediamtx.service` — RTSP 8554, RTMP 1935, SRT 8890/udp, WebRTC 8889 + 8189/udp, playback 9996
- `rtak-caddy.service` — HTTPS 80/443 (only enabled when a domain is configured)
- `rtak.target` — start/stop everything as one unit

Hardened by default: `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
explicit `ReadWritePaths`, and `CAP_NET_BIND_SERVICE` only for Caddy (so nothing runs as root).

## 6. `install.sh` — the whole UX

```bash
sudo ./install.sh                     # interactive: asks host, password, HTTPS
sudo ./install.sh --yes               # non-interactive, sensible defaults
sudo ./install.sh --host rtak.example.com --domain rtak.example.com --enable-https
```

It: detects arch → creates the `rtak` user → installs files → writes `/etc/rtak/rtak.env`
(generating a strong admin password if none given) → generates the CA + server certificate →
installs and enables the systemd units → opens the firewall (ufw, if present) → waits for the
health check → prints the URL and credentials. Re-running it upgrades in place.

## 7. `rtak` management CLI

One verb-based command so the operator never has to remember systemd syntax:

`rtak status | start | stop | restart | logs [-f] | doctor | backup | restore | enroll | version | uninstall`

`rtak doctor` is the self-test: checks services, ports, certificate validity/expiry, disk space,
database integrity and public reachability — the thing to run before calling for help.

## 8. Testing strategy

Docker is used **only as a disposable Linux machine** to test the native install:

1. **Zero-dependency install** on bare `ubuntu:24.04` (no python/openssl/systemd) — proves the
   bundle needs nothing.
2. **systemd install** in a systemd-enabled container — proves the units, boot-enable and
   `rtak` CLI work.
3. **Functional parity suite** — the same checks the Docker deployment passed: unit tests,
   certificate enrollment over the Marti API, CoT device connect over mTLS 8089, tracking,
   chat, emergency, video publish + WHEP playback, and recording.
4. **Debian 12** run of the same suite for the secondary target.

## 9. Decisions made without asking (unattended build)

| Decision | Rationale | How to change |
|---|---|---|
| Ubuntu Server 24.04 LTS primary | longest support + easiest install | works on Debian 12 as-is |
| Vendor Python + openssl | bare images lack both; makes install unconditional | prefers system openssl when present |
| `/opt/rtak` + `/etc/rtak` + `/var/lib/rtak` | FHS-correct; lets upgrade preserve data | paths are constants at the top of `install.sh` |
| Dedicated `rtak` user, not root | least privilege | — |
| HTTPS off unless a domain is given | Let's Encrypt needs a real public name | `rtak` CLI / re-run installer to enable |
| No Docker in this project | it is the thing being replaced | Docker version remains in the TAK - Revamp repo |
