#!/usr/bin/env bash
# Generate a local CA + server certificate for the TAK TLS listener (8089).
# Usage: scripts/make-certs.sh [server-hostname-or-ip]
#        scripts/make-certs.sh --import <fullchain.pem> <privkey.pem>
#
# Phase 3 replaces this with the built-in CA + enrollment service; this
# script is enough to bring up TLS for testing with real ATAK/iTAK clients.
set -euo pipefail

DIR="${CERTS_DIR:-$(cd "$(dirname "$0")/.." && pwd)/server/certs}"

DAYS=1825        # CA validity (CAs are exempt from Apple's leaf limit)
# iOS/iPadOS reject TLS *server* certificates valid for more than 825 days
# (Apple HT210176, enforced since iOS 13) — the handshake is silently
# dropped, which iTAK surfaces as a generic "error connecting to the
# server" even when the CA is fully trusted. Keep the leaf under that cap.
LEAF_DAYS=820

# Creates ca.key/ca.pem in the current directory if they don't already exist,
# with the v3 extensions Android/ATAK's TLS stack (Conscrypt) requires on a
# CA cert: basicConstraints CA:TRUE, keyCertSign, and a Subject Key Id so the
# server cert can carry a matching Authority Key Id for path building.
#
# Unique CA name per generation. ATAK stores every imported CA by name; if
# two CAs share a name (as happened across regenerations) ATAK can match a
# stale one and reject the server with "certificate_unknown". A unique CN
# guarantees the new CA never collides with an old one left on a device.
ensure_ca() {
  local msg="${1:-creating CA}"
  # Always randomize CA_ID/CA_CN, even when reusing an existing ca.pem: the
  # self-signed flow uses CA_CN as the truststore.p12 friendly name below,
  # and previously did so on every run regardless of whether the CA itself
  # was freshly minted.
  CA_ID="$(openssl rand -hex 3)"
  CA_CN="TAK-Revamp-CA-${CA_ID}"
  if [[ -f ca.pem ]]; then
    return
  fi
  echo ">> ${msg}"
  openssl genrsa -out ca.key 4096
  cat > ca_ext.cnf <<EOF
[req]
distinguished_name = dn
x509_extensions = v3_ca
prompt = no
[dn]
C = US
O = TAK-Revamp
CN = ${CA_CN}
[v3_ca]
basicConstraints = critical, CA:TRUE
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF
  openssl req -x509 -new -nodes -key ca.key -sha256 -days "$DAYS" \
    -config ca_ext.cnf -out ca.pem
  rm -f ca_ext.cnf 2>/dev/null || true
  chmod 600 ca.key
}

if [[ "${1:-}" == "--import" ]]; then
  FULLCHAIN="${2:?usage: make-certs.sh --import fullchain.pem privkey.pem}"
  PRIVKEY="${3:?usage: make-certs.sh --import fullchain.pem privkey.pem}"

  openssl x509 -noout -in "$FULLCHAIN" >/dev/null 2>&1 || {
    echo "error: $FULLCHAIN is not a valid certificate (expected fullchain.pem)" >&2
    exit 1
  }
  openssl pkey -noout -in "$PRIVKEY" >/dev/null 2>&1 || {
    echo "error: $PRIVKEY is not a valid private key (expected privkey.pem)" >&2
    exit 1
  }

  CERT_PUBKEY="$(openssl x509 -noout -pubkey -in "$FULLCHAIN")" || {
    echo "error: failed to extract public key from $FULLCHAIN" >&2
    exit 1
  }
  KEY_PUBKEY="$(openssl pkey -pubout -in "$PRIVKEY" 2>/dev/null)" || {
    echo "error: failed to derive public key from $PRIVKEY" >&2
    exit 1
  }
  if [[ "$CERT_PUBKEY" != "$KEY_PUBKEY" ]]; then
    echo "error: $FULLCHAIN and $PRIVKEY do not match (public keys differ)" >&2
    exit 1
  fi

  mkdir -p "$DIR"
  cp "$FULLCHAIN" "$DIR/.server.pem.tmp"
  cp "$PRIVKEY"   "$DIR/.server.key.tmp"
  chmod 600 "$DIR/.server.key.tmp"
  mv "$DIR/.server.pem.tmp" "$DIR/server.pem"
  mv "$DIR/.server.key.tmp" "$DIR/server.key"

  cd "$DIR"
  # Local CA still needed to sign *client* certs for mTLS enrollment.
  ensure_ca "creating client-signing CA (server cert is external)"
  echo "Imported external server certificate into $DIR"
  exit 0
fi

mkdir -p "$DIR"
# One or more addresses may be given; the FIRST becomes the cert CN and every
# one becomes a SAN. Mixing a DNS name and an IP lets a single cert serve the
# DDNS hostname (remote) and the LAN IP (on-LAN, when the router won't hairpin
# the public name):   make-certs.sh rtak.ddns.net 192.168.2.190
HOSTS=("$@")
[[ ${#HOSTS[@]} -eq 0 ]] && HOSTS=("$(hostname -f 2>/dev/null || hostname)")
HOST="${HOSTS[0]}"
cd "$DIR"

ensure_ca
SUBJ_SRV="/C=US/O=TAK-Revamp/CN=${HOST}"

echo ">> creating server cert for ${HOST}"
openssl genrsa -out server.key 2048
openssl req -new -key server.key -subj "$SUBJ_SRV" -out server.csr

# SAN: each host goes in as IP: when it's a dotted-quad, DNS: when it's a name.
# Getting this wrong makes ATAK/iTAK silently refuse the TLS connection
# ("Registration failed") because the cert doesn't match the address dialed.
SAN_LINE=""
for h in "${HOSTS[@]}"; do
  if [[ "$h" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    SAN_LINE="${SAN_LINE:+$SAN_LINE, }IP:${h}"
  else
    SAN_LINE="${SAN_LINE:+$SAN_LINE, }DNS:${h}"
  fi
done
# Full v3 leaf extensions. Missing SKI/AKI/keyUsage is the usual cause of
# Android's "certificate_unknown" TLS alert during enrollment.
cat > san.cnf <<EOF
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth, clientAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid, issuer
subjectAltName = ${SAN_LINE}, DNS:localhost, IP:127.0.0.1
EOF
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial \
  -days "$LEAF_DAYS" -sha256 -extfile san.cnf -out server.pem
rm -f server.csr san.cnf 2>/dev/null || true

# Truststore for ATAK/iTAK import (password: atakatak, the TAK convention).
# MUST use the classic PKCS12 format: PBE-SHA1-RC2-40 for the cert + SHA1 MAC.
# This exactly matches what TAK Server / OpenTAKServer emit and what current
# ATAK (verified on v5.6) accepts. 3DES and OpenSSL-3 defaults (AES-256/SHA256)
# are silently REJECTED by modern ATAK — the CA never loads and every SSL
# connect fails with "identity could not be verified" / no server contact.
# RC2 lives in OpenSSL 3's legacy provider, so -legacy is required to build it.
openssl pkcs12 -export -legacy -nokeys -in ca.pem -out truststore.p12 \
  -passout pass:atakatak -name "${CA_CN}" \
  -certpbe PBE-SHA1-RC2-40 -macalg sha1 2>/dev/null \
|| openssl pkcs12 -export -nokeys -in ca.pem -out truststore.p12 \
  -passout pass:atakatak -name "${CA_CN}" -legacy 2>/dev/null \
|| openssl pkcs12 -export -nokeys -in ca.pem -out truststore.p12 \
  -passout pass:atakatak -name "${CA_CN}"

echo
echo "Done. Files in server/certs/:"
echo "  ca.pem / ca.key      - local CA (keep ca.key safe)"
echo "  server.pem / .key    - TLS cert for the 8089 listener"
echo "  truststore.p12       - import into ATAK/iTAK (password: atakatak)"
