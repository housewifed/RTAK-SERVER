#!/usr/bin/env bash
# RTAK Server installer - self-contained, offline, idempotent.
#
#   sudo ./install.sh                      interactive
#   sudo ./install.sh --yes                accept sensible defaults
#   sudo ./install.sh --host rtak.example.com --domain rtak.example.com
#
# Re-running upgrades the program files and LEAVES YOUR DATA ALONE
# (devices, certificates, recordings and settings are preserved).
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PAY="$SRC/payload"
PREFIX=/opt/rtak
ETC=/etc/rtak
DATA=/var/lib/rtak
UNIT_DIR=/etc/systemd/system
RTAK_USER=rtak

c_g=$'\033[1;32m'; c_r=$'\033[1;31m'; c_y=$'\033[1;33m'; c_b=$'\033[1;36m'; c_d=$'\033[2m'; c_0=$'\033[0m'
say()  { printf "\n${c_b}==>${c_0} %s\n" "$*"; }
ok()   { printf "    ${c_g}OK${c_0} %s\n" "$*"; }
warn() { printf "    ${c_y}!!${c_0} %s\n" "$*"; }
die()  { printf "\n${c_r}ERROR:${c_0} %s\n\n" "$*" >&2; exit 1; }

OPT_HOST=""; OPT_DOMAIN=""; OPT_PW=""; OPT_YES=0; OPT_FW=1; OPT_SERVICE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --host)            OPT_HOST="${2:?}"; shift 2 ;;
    --domain)          OPT_DOMAIN="${2:?}"; shift 2 ;;
    --admin-password)  OPT_PW="${2:?}"; shift 2 ;;
    -y|--yes)          OPT_YES=1; shift ;;
    --no-firewall)     OPT_FW=0; shift ;;
    --no-service)      OPT_SERVICE=0; shift ;;   # install files only (containers / WSL)
    -h|--help)
      sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1  (try --help)" ;;
  esac
done

[ "$(id -u)" -eq 0 ] || die "please run with sudo:  sudo $0 $*"
[ -d "$PAY" ] || die "payload/ not found next to install.sh - is the bundle complete?"

VERSION="$(cat "$SRC/VERSION" 2>/dev/null || echo 0.0.0)"
BUNDLE_ARCH="$(cat "$PAY/ARCH" 2>/dev/null || echo unknown)"
case "$(uname -m)" in
  x86_64|amd64) HOST_ARCH=amd64 ;;
  aarch64|arm64) HOST_ARCH=arm64 ;;
  *) die "unsupported CPU architecture: $(uname -m) (need x86_64 or aarch64)" ;;
esac
[ "$BUNDLE_ARCH" = "$HOST_ARCH" ] || die "this bundle is for $BUNDLE_ARCH but the machine is $HOST_ARCH - download the $HOST_ARCH bundle"

printf "\n${c_b}RTAK Server %s${c_0}  (%s)\n" "$VERSION" "$HOST_ARCH"
UPGRADE=0; [ -f "$ETC/rtak.env" ] && UPGRADE=1
[ "$UPGRADE" = 1 ] && printf "${c_d}Existing installation found - upgrading, your data is preserved.${c_0}\n"

HAVE_SYSTEMD=0
if [ "$OPT_SERVICE" = 1 ] && command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
  HAVE_SYSTEMD=1
elif [ "$OPT_SERVICE" = 1 ]; then
  warn "systemd not available here - installing files only (no auto-start)"
fi

# ---------------------------------------------------------------- settings ---
# On upgrade keep whatever is already configured; only ask for what is missing.
EX_HOST=""; EX_DOMAIN=""; EX_PW=""; EX_USER=""; EX_PT=""; EX_SS=""
if [ "$UPGRADE" = 1 ]; then
  # shellcheck disable=SC1090
  EX_HOST=$(grep -E '^SERVER_HOST=' "$ETC/rtak.env" | cut -d= -f2- || true)
  EX_DOMAIN=$(grep -E '^TAK_DOMAIN=' "$ETC/rtak.env" | cut -d= -f2- || true)
  EX_PW=$(grep -E '^ADMIN_PASSWORD=' "$ETC/rtak.env" | cut -d= -f2- || true)
  EX_USER=$(grep -E '^ADMIN_USER=' "$ETC/rtak.env" | cut -d= -f2- || true)
  EX_PT=$(grep -E '^PUBLISH_TOKEN=' "$ETC/rtak.env" | cut -d= -f2- || true)
  EX_SS=$(grep -E '^STREAM_TOKEN_SECRET=' "$ETC/rtak.env" | cut -d= -f2- || true)
fi

PYBIN="$PAY/runtime/python/bin/python3"
detect_ip() { "$PYBIN" -c 'import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
try:
    s.connect(("192.0.2.1",80)); print(s.getsockname()[0])
except Exception: print("127.0.0.1")
finally: s.close()' 2>/dev/null || echo 127.0.0.1; }

DEFAULT_HOST="${OPT_HOST:-${EX_HOST:-$(detect_ip)}}"
INTERACTIVE=0; [ "$OPT_YES" = 0 ] && [ -t 0 ] && INTERACTIVE=1

SERVER_HOST="$DEFAULT_HOST"
TAK_DOMAIN="${OPT_DOMAIN:-${EX_DOMAIN:-localhost}}"
ADMIN_USER="${EX_USER:-admin}"

if [ "$INTERACTIVE" = 1 ]; then
  say "Configuration"
  printf "  Address devices will use to reach this server\n"
  printf "  (LAN IP, or a DDNS name like rtak.example.com)\n"
  printf "  [%s]: " "$DEFAULT_HOST"; read -r a; SERVER_HOST="${a:-$DEFAULT_HOST}"
  if [ -z "$OPT_DOMAIN" ]; then
    printf "\n  Enable HTTPS with a free Let's Encrypt certificate?\n"
    printf "  Needs a public domain name pointing here, plus ports 80+443 forwarded.\n"
    printf "  Domain (blank = skip, use plain http on :8080) [%s]: " "${EX_DOMAIN:-}"
    read -r a; TAK_DOMAIN="${a:-${EX_DOMAIN:-localhost}}"
    [ -z "$TAK_DOMAIN" ] && TAK_DOMAIN=localhost
  fi
fi

# Use the bundled interpreter: `tr </dev/urandom | head -c N` makes tr exit on
# SIGPIPE, which would fire the fallback too and concatenate both outputs.
gen() { "$PYBIN" -c "import secrets,string,sys
a=string.ascii_letters+string.digits
sys.stdout.write(''.join(secrets.choice(a) for _ in range(${1:-24})))"; }
ADMIN_PASSWORD="${OPT_PW:-${EX_PW:-}}"; NEW_PW=0
if [ -z "$ADMIN_PASSWORD" ]; then ADMIN_PASSWORD="$(gen 20)"; NEW_PW=1; fi
PUBLISH_TOKEN="${EX_PT:-$(gen 24)}"
STREAM_TOKEN_SECRET="${EX_SS:-$(gen 48)}"

# ------------------------------------------------------------------ user ----
say "Creating the service account"
if id "$RTAK_USER" >/dev/null 2>&1; then ok "user '$RTAK_USER' already exists"
else
  useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$RTAK_USER" 2>/dev/null \
    || useradd --system --home-dir "$DATA" --shell /bin/false "$RTAK_USER"
  ok "created system user '$RTAK_USER' (no login, no password)"
fi

# --------------------------------------------------------------- program ----
say "Installing program files to $PREFIX"
if [ "$HAVE_SYSTEMD" = 1 ] && [ "$UPGRADE" = 1 ]; then
  systemctl stop rtak-takcore rtak-mediamtx rtak-caddy 2>/dev/null || true
fi
rm -rf "$PREFIX/app" "$PREFIX/runtime" "$PREFIX/bin" "$PREFIX/config" "$PREFIX/scripts"
mkdir -p "$PREFIX"
cp -a "$PAY/." "$PREFIX/"
rm -f "$PREFIX/ARCH"
cp -f "$SRC/VERSION" "$PREFIX/VERSION"
cp -f "$SRC/uninstall.sh" "$PREFIX/uninstall.sh"; chmod +x "$PREFIX/uninstall.sh"
chmod +x "$PREFIX"/bin/* "$PREFIX"/scripts/*.sh 2>/dev/null || true
ok "installed $(du -sh "$PREFIX" 2>/dev/null | cut -f1) of program files"

mkdir -p "$ETC/certs" "$DATA/recordings" "$DATA/caddy"
chown -R "$RTAK_USER:$RTAK_USER" "$DATA"
chmod 750 "$DATA"

# ---------------------------------------------------------------- openssl ---
say "Selecting an openssl"
VEND="$PREFIX/runtime/openssl"
make_shim() { printf '#!/bin/sh\nexec %s "$@"\n' "$1" > "$PREFIX/bin/openssl"; chmod +x "$PREFIX/bin/openssl"; }
if [ -x "$VEND/bin/openssl.real" ] && LD_LIBRARY_PATH="$VEND/lib" "$VEND/bin/openssl.real" version >/dev/null 2>&1; then
  # The shim pins the bundled libraries, config and providers so the CLI works
  # on a machine that has no OpenSSL of its own.
  printf '#!/bin/sh\nexec env LD_LIBRARY_PATH=%s/lib OPENSSL_CONF=%s/openssl.cnf OPENSSL_MODULES=%s/lib/ossl-modules %s/bin/openssl.real "$@"\n' \
    "$VEND" "$VEND" "$VEND" "$VEND" > "$PREFIX/bin/openssl"
  chmod +x "$PREFIX/bin/openssl"
  ok "using the bundled openssl ($("$PREFIX/bin/openssl" version 2>/dev/null))"
elif command -v openssl >/dev/null 2>&1; then
  make_shim "$(command -v openssl)"
  ok "using the system openssl ($(openssl version 2>/dev/null))"
else
  die "no usable openssl found (bundled copy failed and none installed)"
fi

# ------------------------------------------------------------------- env ----
say "Writing settings to $ETC/rtak.env"
umask 077
cat > "$ETC/rtak.env" <<ENVEOF
# RTAK Server settings.  Edit, then:  sudo rtak restart
# Generated $(date -u '+%Y-%m-%d %H:%M:%SZ') by install.sh $VERSION

# Address devices use to reach this server (goes into enrollment QR codes and
# the TLS certificate). Change it with: sudo rtak renew-certs
SERVER_HOST=$SERVER_HOST

# Web UI login.
ADMIN_USER=$ADMIN_USER
ADMIN_PASSWORD=$ADMIN_PASSWORD
AUTH_ENABLED=1

# Shared secret phones present to publish video (embedded in the setup QRs).
PUBLISH_TOKEN=$PUBLISH_TOKEN
# Signs short-lived WebRTC playback tickets.
STREAM_TOKEN_SECRET=$STREAM_TOKEN_SECRET

# mTLS on the CoT port is required. Set to 0 only if a device cannot present
# a client certificate (weakens security).
REQUIRE_CLIENT_CERT=1
# Plain-TCP CoT is a cert-less bypass; 0 = disabled (recommended).
TAK_TCP_PORT=0

# Ports
TAK_TLS_PORT=8089
HTTP_PORT=8080
ENROLL_PORT=8446

# Paths
DB_PATH=$DATA/takcore.db
WEB_DIR=$PREFIX/app/web
CERT_FILE=$ETC/certs/server.pem
KEY_FILE=$ETC/certs/server.key
CA_FILE=$ETC/certs/ca.pem
CERTS_DIR=$ETC/certs

# Video engine (local)
MEDIAMTX_API=http://127.0.0.1:9997
MEDIAMTX_PLAYBACK=http://127.0.0.1:9996
MTX_WEBRTCADDITIONALHOSTS=$SERVER_HOST

# HTTPS front door. "localhost" = internal certificate (no public ports).
# Set a real domain + forward ports 80/443 for a Let's Encrypt certificate.
TAK_DOMAIN=$TAK_DOMAIN

# openssl shim first so takcore finds the bundled copy.
PATH=$PREFIX/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENVEOF
chmod 640 "$ETC/rtak.env"; chown root:"$RTAK_USER" "$ETC/rtak.env"
umask 022
ok "settings written (readable only by root and $RTAK_USER)"

# ----------------------------------------------------------- certificates ---
say "Certificates"
if [ -f "$ETC/certs/ca.pem" ] && [ -f "$ETC/certs/server.pem" ]; then
  CUR_SANS=$("$PREFIX/bin/openssl" x509 -in "$ETC/certs/server.pem" -noout -ext subjectAltName 2>/dev/null | tail -1)
  if echo "$CUR_SANS" | grep -q "$SERVER_HOST"; then
    ok "existing certificate already covers $SERVER_HOST (devices keep working)"
  else
    warn "certificate does not cover $SERVER_HOST - re-issuing (same CA, devices keep working)"
    CERTS_DIR="$ETC/certs" PATH="$PREFIX/bin:$PATH" "$PREFIX/scripts/make-certs.sh" "$SERVER_HOST" "$(detect_ip)" >/dev/null
    ok "server certificate re-issued"
  fi
else
  CERTS_DIR="$ETC/certs" PATH="$PREFIX/bin:$PATH" "$PREFIX/scripts/make-certs.sh" "$SERVER_HOST" "$(detect_ip)" >/dev/null
  ok "created certificate authority + server certificate for $SERVER_HOST"
fi
chown -R "$RTAK_USER:$RTAK_USER" "$ETC/certs"; chmod 750 "$ETC/certs"; chmod 640 "$ETC"/certs/* 2>/dev/null || true

# --------------------------------------------------------------- services ---
if [ "$HAVE_SYSTEMD" = 1 ]; then
  say "Installing services"
  cp -f "$PREFIX"/systemd/*.service "$PREFIX"/systemd/*.target "$UNIT_DIR"/
  systemctl daemon-reload
  systemctl enable rtak-takcore rtak-mediamtx >/dev/null 2>&1
  if [ "$TAK_DOMAIN" != "localhost" ] && [ -n "$TAK_DOMAIN" ]; then
    systemctl enable rtak-caddy >/dev/null 2>&1; ok "HTTPS enabled for $TAK_DOMAIN"
  else
    systemctl disable rtak-caddy >/dev/null 2>&1 || true
  fi
  ok "services enabled (they start automatically at boot)"
  systemctl restart rtak-takcore rtak-mediamtx
  [ "$TAK_DOMAIN" != "localhost" ] && [ -n "$TAK_DOMAIN" ] && systemctl restart rtak-caddy || true
else
  say "Skipping services (no systemd)"
  warn "start manually:  PATH=$PREFIX/bin:\$PATH $PREFIX/runtime/python/bin/python3 -m takcore"
fi

# --------------------------------------------------------------- firewall ---
if [ "$OPT_FW" = 1 ] && command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
  say "Opening firewall ports (ufw)"
  for p in 8080/tcp 8089/tcp 8446/tcp 8554/tcp 1935/tcp 8889/tcp 9996/tcp 8890/udp 8189/udp; do
    ufw allow "$p" >/dev/null 2>&1 || true
  done
  if [ "$TAK_DOMAIN" != "localhost" ]; then ufw allow 80/tcp >/dev/null 2>&1; ufw allow 443/tcp >/dev/null 2>&1; fi
  ok "ufw rules added"
fi

ln -sf "$PREFIX/bin/rtak" /usr/local/bin/rtak

# ------------------------------------------------------------------ check ---
if [ "$HAVE_SYSTEMD" = 1 ]; then
  say "Checking the server came up"
  if "$PREFIX/bin/rtak-util" waitport 8080 25; then ok "web UI is answering on port 8080"
  else warn "web UI did not answer within 25s - check:  rtak logs takcore"; fi
  "$PREFIX/bin/rtak-util" waitport 8554 10 >/dev/null 2>&1 && ok "video engine is up" || warn "video engine slow to start - check: rtak logs mediamtx"
fi

IP="$(detect_ip)"
cat <<SUMMARY

${c_g}============================================================${c_0}
${c_g} RTAK Server $VERSION is installed${c_0}
${c_g}============================================================${c_0}

  Open the web UI:   ${c_b}http://$IP:8080${c_0}
SUMMARY
[ "$TAK_DOMAIN" != "localhost" ] && [ -n "$TAK_DOMAIN" ] && printf "                     ${c_b}https://%s${c_0}   (Let's Encrypt)\n" "$TAK_DOMAIN"
cat <<SUMMARY

  Username:          $ADMIN_USER
SUMMARY
if [ "$NEW_PW" = 1 ]; then
  printf "  Password:          ${c_y}%s${c_0}\n" "$ADMIN_PASSWORD"
  printf "                     ${c_d}^ save this now - shown only once${c_0}\n"
else
  printf "  Password:          (unchanged)\n"
fi
cat <<SUMMARY

  Devices connect to: $SERVER_HOST:8089   (enrollment on :8446)

  Useful commands:
    rtak status      is everything running?
    rtak doctor      full self-test
    rtak enroll      create a device enrollment token
    rtak logs -f     watch the logs
    rtak backup      save database + certificates

SUMMARY
