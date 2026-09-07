"""Certificate enrollment: the Marti API subset that ATAK/iTAK use when
enrolling via Quick Connect / a ``tak://`` QR code, plus token management.

Flow (what the device does after scanning the QR):
  1. GET  https://server:8446/Marti/api/tls/config           (CSR subject info)
  2. POST https://server:8446/Marti/api/tls/signClient/v2    (Basic auth
     username:token, body = base64 PKCS#10 CSR) -> JSON {signedCert, ca0}
  3. GET  https://server:8446/Marti/api/tls/profile/enrollment (optional
     post-enroll preference package; we return 204 for now)
  4. Device connects to :8089 TLS using the signed client certificate.

Runs on its own HTTPS port (default 8446, the standard TAK cert-enrollment
port) using the same server certificate as the CoT TLS listener.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from .ca import CAError, CertificateAuthority, subject_cn
from .store import Store

log = logging.getLogger("takcore.enroll")

TLS_CONFIG_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<ns2:certificateConfig xmlns="http://bbn.com/marti/xml/config" '
    'xmlns:ns2="com.bbn.marti.config">'
    "<nameEntries>"
    '<nameEntry name="O" value="TAK-Revamp"/>'
    '<nameEntry name="OU" value="TAK-Revamp"/>'
    "</nameEntries>"
    "</ns2:certificateConfig>"
)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class EnrollmentService:
    """Token issue/verify + certificate signing logic (transport-agnostic)."""

    def __init__(self, store: Store, ca: CertificateAuthority) -> None:
        self.store = store
        self.ca = ca

    # -- trust artifacts ------------------------------------------------------

    @property
    def truststore_path(self) -> str:
        return os.path.join(self.ca.dir, "truststore.p12")

    def truststore_bytes(self) -> Optional[bytes]:
        try:
            with open(self.truststore_path, "rb") as f:
                return f.read()
        except OSError:
            return None

    def ios_trust_profile(self) -> Optional[bytes]:
        """iOS configuration profile (.mobileconfig) installing the CA.

        iOS refuses TLS to a server signed by an unknown CA (iTAK shows a
        generic connection error), and unlike ATAK there is no data package
        that can deliver the truststore. A profile is Apple's supported
        path: install it once, then enable full trust in Settings →
        General → About → Certificate Trust Settings.
        """
        import plistlib
        import uuid
        if not self.ca.exists():
            return None
        der = self.ca.ca_der()
        name = subject_cn(self.ca.ca_pem()) or "TAK-Revamp-CA"
        # UUIDs derived from the cert so the same CA always yields the same
        # profile (reinstall updates instead of stacking duplicates)
        ns = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        cert_uuid = str(uuid.uuid5(ns, "cert:" + der.hex())).upper()
        prof_uuid = str(uuid.uuid5(ns, "prof:" + der.hex())).upper()
        profile = {
            "PayloadContent": [{
                "PayloadCertificateFileName": "ca.cer",
                "PayloadContent": der,
                "PayloadDescription": "Adds the TAK Revamp server CA",
                "PayloadDisplayName": name,
                "PayloadIdentifier":
                    f"io.takrevamp.ca.{cert_uuid}",
                "PayloadType": "com.apple.security.root",
                "PayloadUUID": cert_uuid,
                "PayloadVersion": 1,
            }],
            "PayloadDescription":
                "Trust the TAK Revamp server so iTAK can connect",
            "PayloadDisplayName": f"TAK Revamp trust ({name})",
            "PayloadIdentifier": f"io.takrevamp.profile.{prof_uuid}",
            "PayloadRemovalDisallowed": False,
            "PayloadType": "Configuration",
            "PayloadUUID": prof_uuid,
            "PayloadVersion": 1,
        }
        return plistlib.dumps(profile)

    def data_package(self, host: str, callsign: Optional[str] = None
                     ) -> Optional[bytes]:
        """Build an ATAK auto-enrollment data package, or None if the
        truststore is missing."""
        from .datapackage import build_enrollment_package
        ts = self.truststore_bytes()
        if ts is None:
            return None
        return build_enrollment_package(host, ts, callsign=callsign)

    def softcert_package(self, host: str, callsign: Optional[str] = None
                         ) -> Optional[bytes]:
        """Build a zero-typing data package: server signs a client cert and
        bundles it so the device connects immediately, no token prompt."""
        from .datapackage import build_softcert_package
        ts = self.truststore_bytes()
        if ts is None:
            return None
        cn = callsign or f"device-{secrets.token_hex(3)}"
        client_p12 = self.ca.make_client_p12(cn)
        return build_softcert_package(host, ts, client_p12, callsign=callsign)

    # -- tokens ---------------------------------------------------------------

    def create_token(self, username: Optional[str] = None,
                     callsign: Optional[str] = None,
                     expires_hours: Optional[float] = 24.0,
                     max_uses: Optional[int] = None) -> Dict[str, Any]:
        # max_uses defaults to None (valid until it expires) rather than 1:
        # TAK enrollment makes several HTTP requests (ATAK re-fetches the
        # package; iTAK does config+signClient) and users retry, so a strict
        # single-use token made the QR work once and then 403. The token still
        # gates access and expires on its own (default 24h).
        username = (username or f"device-{secrets.token_hex(3)}").strip()
        token = secrets.token_urlsafe(9)
        expires = time.time() + expires_hours * 3600 if expires_hours else None
        self.store.create_enroll_user(username, hash_token(token), callsign,
                                      expires, max_uses)
        return {"username": username, "token": token, "callsign": callsign,
                "expires": expires, "max_uses": max_uses}

    def verify(self, username: str, token: str) -> bool:
        user = self.store.get_enroll_user(username)
        if user is None:
            return False
        if user["token_hash"] != hash_token(token):
            return False
        if user["expires"] is not None and time.time() > user["expires"]:
            log.warning("enrollment token for %s expired", username)
            return False
        if user["max_uses"] is not None and user["uses"] >= user["max_uses"]:
            log.warning("enrollment token for %s exhausted", username)
            return False
        return True

    def consume(self, username: str) -> None:
        """Mark one use of an enrollment token (single-use enforcement for the
        softcert download path, mirroring what sign() does for signClient)."""
        self.store.consume_enrollment(username)

    # -- signing --------------------------------------------------------------

    def sign(self, username: str, csr_body: bytes) -> Dict[str, str]:
        cert_pem = self.ca.sign_csr(csr_body)
        self.store.consume_enrollment(username)
        log.info("issued client certificate for %s", username)
        ca_b64 = self.ca.pem_body(self.ca.ca_pem())
        # iTAK builds its server-trust store from the ca0, ca1, ... chain in
        # this response and aborts enrollment (never reaching the CoT port)
        # if ca1 is absent. With a single self-signed CA there is no
        # intermediate, so ca1 duplicates ca0 — exactly what TAK Server and
        # OpenTAKServer return, and what iTAK accepts. ATAK ignores the
        # extra key.
        return {
            "signedCert": self.ca.pem_body(cert_pem),
            "ca0": ca_b64,
            "ca1": ca_b64,
        }


def make_marti_handler(svc: EnrollmentService):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "takcore-enroll/0.1"

        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

        # -- helpers ----------------------------------------------------------

        def _send(self, status: int, body: bytes = b"",
                  ctype: str = "application/json") -> None:
            self.send_response(status)
            if body:
                self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _basic_auth(self) -> Optional[tuple]:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return None
            try:
                decoded = base64.b64decode(header[6:]).decode()
                username, _, token = decoded.partition(":")
                return (username, token)
            except Exception:  # noqa: BLE001
                return None

        def _unauthorized(self) -> None:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="takcore"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        # -- routes -----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            log.info("enroll GET %s", self.path)
            try:
                if path == "/Marti/api/tls/config":
                    self._send(200, TLS_CONFIG_XML.encode(), "application/xml")
                elif path == "/Marti/api/tls/profile/enrollment":
                    self._send(204)  # no post-enroll profile package (yet)
                elif path == "/Marti/api/version/config":
                    body = json.dumps({"version": "3", "type": "ServerConfig",
                                       "data": {"version": "takcore-0.1",
                                                "api": "3", "hostname": ""},
                                       "nodeId": "takcore"}).encode()
                    self._send(200, body)
                elif path == "/Marti/api/clientEndPoints":
                    self._send(200, json.dumps({"version": "3",
                                                "type": "com.bbn.marti.remote.ClientEndpoint",
                                                "data": []}).encode())
                else:
                    self._send(404, b'{"error":"not found"}')
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001
                log.exception("enroll GET %s failed", path)
                self._send(500, b'{"error":"internal"}')

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            creds = self._basic_auth()
            accept = self.headers.get("Accept", "")
            log.info("enroll POST %s (auth=%s user=%s accept=%s)", self.path,
                     "yes" if creds else "no",
                     creds[0] if creds else "-", accept or "-")
            try:
                if path in ("/Marti/api/tls/signClient/v2",
                            "/Marti/api/tls/signClient"):
                    if not creds:
                        log.warning("signClient rejected: no Basic auth header")
                        self._unauthorized()
                        return
                    if not svc.verify(*creds):
                        log.warning("signClient rejected: token invalid/expired/"
                                    "used for user '%s'", creds[0])
                        self._unauthorized()
                        return
                    length = int(self.headers.get("Content-Length", 0))
                    if length <= 0 or length > 1_000_000:
                        log.warning("signClient: bad Content-Length %s", length)
                        self._send(400, b'{"error":"missing CSR"}')
                        return
                    csr = self.rfile.read(length)
                    log.info("signClient: received %d-byte CSR from '%s'",
                             len(csr), creds[0])
                    try:
                        result = svc.sign(creds[0], csr)
                    except CAError as e:
                        log.error("CSR signing failed: %s", e)
                        self._send(400, b'{"error":"bad CSR"}')
                        return
                    log.info("signClient: issued certificate to '%s' OK", creds[0])
                    # iTAK sends Accept: text/plain and wants the JSON body
                    # served with that content type; ATAK is happy with
                    # application/json. Echo whichever the client asked for.
                    ctype = ("text/plain" if "text/plain" in accept
                             else "application/json")
                    self._send(200, json.dumps(result).encode(), ctype)
                else:
                    log.warning("enroll POST unmatched path: %s", path)
                    self._send(404, b'{"error":"not found"}')
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001
                log.exception("enroll POST %s failed", path)
                self._send(500, b'{"error":"internal"}')

    return Handler


def build_chain_file(certfile: str, cafile: str) -> str:
    """Concatenate leaf + CA into a fullchain PEM so clients that don't
    already trust the CA can still build a path. Returns the chain path
    (falls back to the leaf alone if the CA isn't found)."""
    if not os.path.isfile(cafile):
        return certfile
    chain_path = os.path.join(os.path.dirname(certfile) or ".",
                              "server-fullchain.pem")
    try:
        with open(certfile) as f:
            leaf = f.read()
        with open(cafile) as f:
            ca = f.read()
        with open(chain_path, "w") as f:
            f.write(leaf.rstrip() + "\n" + ca.lstrip())
        return chain_path
    except OSError:
        return certfile


class _TLSHTTPServer(ThreadingHTTPServer):
    """HTTPS server that wraps each accepted connection individually and
    logs TLS handshake failures instead of silently dropping them (the
    stdlib default swallows ssl.SSLError as a plain OSError)."""

    ssl_context: ssl.SSLContext

    def get_request(self):
        sock, addr = self.socket.accept()
        try:
            tls = self.ssl_context.wrap_socket(sock, server_side=True)
        except ssl.SSLError as e:
            log.warning("TLS handshake from %s failed: %s", addr[0], e)
            try:
                sock.close()
            except OSError:
                pass
            raise  # socketserver ignores this connection
        except OSError as e:
            try:
                sock.close()
            except OSError:
                pass
            raise
        return tls, addr


def start_enrollment_server(svc: EnrollmentService, port: int,
                            certfile: str, keyfile: str,
                            cafile: Optional[str] = None
                            ) -> Optional[ThreadingHTTPServer]:
    handler = make_marti_handler(svc)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    chain = build_chain_file(certfile, cafile) if cafile else certfile
    ctx.load_cert_chain(chain, keyfile)
    httpd = _TLSHTTPServer(("0.0.0.0", port), handler)
    httpd.ssl_context = ctx
    thread = threading.Thread(target=httpd.serve_forever, daemon=True,
                              name="takcore-enroll")
    thread.start()
    log.info("certificate enrollment (Marti API) on https://0.0.0.0:%d", port)
    return httpd
