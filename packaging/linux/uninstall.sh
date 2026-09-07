#!/usr/bin/env bash
# Remove RTAK Server.  sudo rtak uninstall  (or: sudo /opt/rtak/uninstall.sh)
#   --purge  also delete devices, certificates, recordings and settings
#   --yes    do not ask
set -euo pipefail
PREFIX=/opt/rtak; ETC=/etc/rtak; DATA=/var/lib/rtak; UNIT_DIR=/etc/systemd/system
c_y=$'\033[1;33m'; c_g=$'\033[1;32m'; c_0=$'\033[0m'
PURGE=0; YES=0
for a in "$@"; do case "$a" in --purge) PURGE=1;; -y|--yes) YES=1;; esac; done
[ "$(id -u)" -eq 0 ] || { echo "run with sudo"; exit 1; }

echo "This will remove the RTAK Server program files."
if [ "$PURGE" = 1 ]; then
  printf "${c_y}--purge given: your devices, certificates, recordings and settings will ALSO be deleted.${c_0}\n"
else
  echo "Your data is KEPT in $ETC and $DATA (re-install to pick it back up)."
fi
if [ "$YES" != 1 ]; then printf "Type 'yes' to continue: "; read -r a; [ "$a" = yes ] || { echo "Aborted."; exit 1; }; fi

systemctl stop rtak-takcore rtak-mediamtx rtak-caddy 2>/dev/null || true
systemctl disable rtak-takcore rtak-mediamtx rtak-caddy 2>/dev/null || true
rm -f "$UNIT_DIR"/rtak-takcore.service "$UNIT_DIR"/rtak-mediamtx.service \
      "$UNIT_DIR"/rtak-caddy.service "$UNIT_DIR"/rtak.target
systemctl daemon-reload 2>/dev/null || true
rm -f /usr/local/bin/rtak
rm -rf "$PREFIX"

if [ "$PURGE" = 1 ]; then
  rm -rf "$ETC" "$DATA"
  userdel rtak 2>/dev/null || true
  printf "${c_g}RTAK Server fully removed (including all data).${c_0}\n"
else
  printf "${c_g}RTAK Server removed.${c_0}  Data kept in $ETC and $DATA\n"
  echo "Delete it later with:  sudo rm -rf $ETC $DATA && sudo userdel rtak"
fi
