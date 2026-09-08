#!/usr/bin/env bash
# Build a self-contained RTAK Server bundle for Linux.
#
#   ./build-bundle.sh              # both architectures
#   ./build-bundle.sh amd64        # just one
#
# Needs: network + docker (docker is used ONLY to lift the openssl CLI and its
# libraries out of a Debian 12 image, so the result runs on a machine with
# nothing installed). The produced tarball itself installs fully offline.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
CACHE="$HERE/.cache"
OUT="$ROOT/dist"
VERSION="$(cat "$ROOT/VERSION")"

# --- pinned component versions (bump deliberately) --------------------------
PY_TAG=20260901
PY_VER=3.12.14
MTX_VER=1.21.0
CADDY_VER=2.11.4
DEBIAN_IMG=debian:12          # oldest glibc we target -> widest compatibility

c_b=$'\033[1;36m'; c_g=$'\033[1;32m'; c_0=$'\033[0m'
say(){ printf "\n${c_b}==>${c_0} %s\n" "$*"; }
ok(){ printf "    ${c_g}OK${c_0} %s\n" "$*"; }
die(){ printf "\nERROR: %s\n" "$*" >&2; exit 1; }

fetch(){ # fetch <url> <dest>
  local url="$1" dest="$2"
  [ -s "$dest" ] && { ok "cached $(basename "$dest")"; return; }
  mkdir -p "$(dirname "$dest")"
  echo "    downloading $(basename "$dest")"
  curl -fsSL --retry 3 -o "$dest.part" "$url" || die "download failed: $url"
  mv "$dest.part" "$dest"
}

build_one(){
  local ARCH="$1"
  case "$ARCH" in
    amd64) PYARCH=x86_64;  DOCKPLAT=linux/amd64 ;;
    arm64) PYARCH=aarch64; DOCKPLAT=linux/arm64 ;;
    *) die "arch must be amd64 or arm64" ;;
  esac

  say "Building RTAK Server $VERSION for linux/$ARCH"
  local STAGE="$HERE/.stage-$ARCH"
  rm -rf "$STAGE"; mkdir -p "$STAGE/payload"/{app,runtime,bin,config,systemd,scripts}
  local PAY="$STAGE/payload"

  # ---- 1. CPython (relocatable, self-contained) ----
  say "Python $PY_VER"
  local pytar="$CACHE/cpython-$PY_VER-$PYARCH.tar.gz"
  fetch "https://github.com/astral-sh/python-build-standalone/releases/download/$PY_TAG/cpython-$PY_VER+$PY_TAG-$PYARCH-unknown-linux-gnu-install_only.tar.gz" "$pytar"
  tar xzf "$pytar" -C "$PAY/runtime"          # creates runtime/python/
  # Trim what a stdlib-only server never uses (keeps the bundle small).
  rm -rf "$PAY"/runtime/python/lib/python3.12/{test,idlelib,tkinter,turtledemo,ensurepip} \
         "$PAY"/runtime/python/lib/python3.12/lib2to3 2>/dev/null || true
  find "$PAY/runtime/python" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  ok "python $(du -sh "$PAY/runtime/python" | cut -f1)"

  # ---- 2. MediaMTX ----
  say "MediaMTX $MTX_VER"
  local mtxtar="$CACHE/mediamtx-$MTX_VER-$ARCH.tar.gz"
  fetch "https://github.com/bluenviron/mediamtx/releases/download/v$MTX_VER/mediamtx_v${MTX_VER}_linux_${ARCH}.tar.gz" "$mtxtar"
  tar xzf "$mtxtar" -C "$PAY/bin" mediamtx
  chmod +x "$PAY/bin/mediamtx"; ok "mediamtx $(du -h "$PAY/bin/mediamtx" | cut -f1)"

  # ---- 3. Caddy ----
  say "Caddy $CADDY_VER"
  local cdtar="$CACHE/caddy-$CADDY_VER-$ARCH.tar.gz"
  fetch "https://github.com/caddyserver/caddy/releases/download/v$CADDY_VER/caddy_${CADDY_VER}_linux_${ARCH}.tar.gz" "$cdtar"
  tar xzf "$cdtar" -C "$PAY/bin" caddy
  chmod +x "$PAY/bin/caddy"; ok "caddy $(du -h "$PAY/bin/caddy" | cut -f1)"

  # ---- 4. openssl CLI + its libs, lifted from Debian 12 ----
  say "openssl (from $DEBIAN_IMG, $ARCH)"
  local ssltar="$CACHE/openssl-$ARCH.tar"
  if [ ! -s "$ssltar" ]; then
    command -v docker >/dev/null || die "docker is needed to extract openssl"
    docker run --rm --platform "$DOCKPLAT" "$DEBIAN_IMG" sh -c '
      set -e
      apt-get update -qq >/dev/null 2>&1
      apt-get install -y -qq --no-install-recommends openssl >/dev/null 2>&1
      mkdir -p /out/bin /out/lib/ossl-modules
      cp /usr/bin/openssl /out/bin/openssl.real
      for l in $(ldd /usr/bin/openssl | awk "{print \$3}" | grep -E "libssl|libcrypto"); do cp "$l" /out/lib/; done
      # The legacy provider is REQUIRED: make-certs.sh builds the ATAK
      # truststore with PBE-SHA1-RC2-40, which lives in legacy.so.
      cp /usr/lib/*/ossl-modules/*.so /out/lib/ossl-modules/ 2>/dev/null || true
      cp /etc/ssl/openssl.cnf /out/openssl.cnf
      tar cf - -C /out .' > "$ssltar.part" || die "openssl extraction failed"
    mv "$ssltar.part" "$ssltar"
  else ok "cached openssl-$ARCH.tar"; fi
  mkdir -p "$PAY/runtime/openssl"
  tar xf "$ssltar" -C "$PAY/runtime/openssl"
  chmod +x "$PAY/runtime/openssl/bin/openssl.real"
  ok "openssl + $(ls "$PAY/runtime/openssl/lib" | tr '\n' ' ')+ $(ls "$PAY/runtime/openssl/lib/ossl-modules" 2>/dev/null | tr '\n' ' ')"

  # ---- 5. application + packaging ----
  say "Application"
  cp -R "$ROOT/app/takcore" "$PAY/app/"
  cp -R "$ROOT/app/web"     "$PAY/app/"
  find "$PAY/app" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
  cp "$ROOT/packaging/linux/config"/*        "$PAY/config/"
  cp "$ROOT/packaging/linux/systemd"/*       "$PAY/systemd/"
  cp "$ROOT/packaging/linux/bin/rtak"        "$PAY/bin/"
  cp "$ROOT/packaging/linux/bin/rtak-util"   "$PAY/bin/"
  cp "$ROOT/scripts/make-certs.sh"           "$PAY/scripts/"
  chmod +x "$PAY/bin"/* "$PAY/scripts"/*.sh
  echo "$ARCH" > "$PAY/ARCH"
  cp "$ROOT/packaging/linux/install.sh"   "$STAGE/install.sh"
  cp "$ROOT/packaging/linux/uninstall.sh" "$STAGE/uninstall.sh"
  cp "$ROOT/VERSION"                      "$STAGE/VERSION"
  chmod +x "$STAGE"/*.sh
  ok "app + installer staged"

  # ---- 6. tarball ----
  say "Packaging"
  mkdir -p "$OUT"
  local NAME="rtak-server-$VERSION-linux-$ARCH"
  rm -rf "$OUT/$NAME"; mv "$STAGE" "$OUT/$NAME"
  # macOS stamps every file with com.apple.provenance, which cannot be removed
  # (xattr -c is refused on it). bsdtar would store it as a LIBARCHIVE.xattr
  # pax header and GNU tar on the target then prints "Ignoring unknown extended
  # header keyword" for each one, so tell bsdtar not to record xattrs at all.
  local TAR_FLAGS=()
  tar --version 2>/dev/null | grep -qi bsdtar && TAR_FLAGS=(--no-xattrs --no-mac-metadata)
  tar czf "$OUT/$NAME.tar.gz" "${TAR_FLAGS[@]}" -C "$OUT" "$NAME"
  ( cd "$OUT" && shasum -a 256 "$NAME.tar.gz" > "$NAME.tar.gz.sha256" 2>/dev/null \
      || sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256" )
  ok "$OUT/$NAME.tar.gz  ($(du -h "$OUT/$NAME.tar.gz" | cut -f1))"

  # ---- 7. single-file self-extracting installer ----------------------------
  # The simplest possible UX: download ONE file, chmod +x, run it.
  local RUN="$OUT/$NAME.run"
  cat > "$RUN" <<'STUB'
#!/bin/sh
# RTAK Server - self-extracting installer.  Usage:  sudo ./this-file [options]
# Everything needed is inside; no network, no dependencies.
set -e
SKIP=$(awk '/^__RTAK_ARCHIVE__/ { print NR + 1; exit 0; }' "$0")
TMP=$(mktemp -d "${TMPDIR:-/tmp}/rtak-install.XXXXXX")
trap 'rm -rf "$TMP"' EXIT INT TERM
echo "Unpacking..."
tail -n +"$SKIP" "$0" | tar xz -C "$TMP"
DIR=$(find "$TMP" -maxdepth 1 -mindepth 1 -type d | head -1)
[ -x "$DIR/install.sh" ] || { echo "corrupt installer" >&2; exit 1; }
exec "$DIR/install.sh" "$@"
__RTAK_ARCHIVE__
STUB
  cat "$OUT/$NAME.tar.gz" >> "$RUN"
  chmod +x "$RUN"
  ( cd "$OUT" && shasum -a 256 "$NAME.run" > "$NAME.run.sha256" 2>/dev/null \
      || sha256sum "$NAME.run" > "$NAME.run.sha256" )
  ok "$OUT/$NAME.run  ($(du -h "$RUN" | cut -f1))  <- single-file installer"
}

ARCHES=("${@:-amd64 arm64}")
# shellcheck disable=SC2068
for a in ${ARCHES[@]}; do build_one "$a"; done

say "Done"
ls -lh "$OUT"/*.tar.gz 2>/dev/null | awk '{print "    "$9"  "$5}'
