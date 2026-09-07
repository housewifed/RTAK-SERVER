"""Certificate authority: signs client certificates for enrolled devices.

Uses the ``openssl`` CLI via subprocess so the server stays free of
third-party Python dependencies. The CA is created automatically on first
start if it does not exist (same CA the make-certs.sh script creates, so
both paths interoperate).
"""

from __future__ import annotations

import base64
import logging
import os
import re
import secrets
import subprocess
import tempfile
from typing import Optional, Tuple

log = logging.getLogger("takcore.ca")

CLIENT_EXT = """\
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
"""


class CAError(Exception):
    pass


def _run(args, input_bytes: Optional[bytes] = None) -> bytes:
    try:
        proc = subprocess.run(args, input=input_bytes, capture_output=True,
                              timeout=30, check=False)
    except FileNotFoundError as e:
        raise CAError("openssl binary not found on PATH") from e
    if proc.returncode != 0:
        raise CAError(f"openssl failed: {proc.stderr.decode(errors='replace')[:400]}")
    return proc.stdout


class CertificateAuthority:
    def __init__(self, certs_dir: str) -> None:
        self.dir = certs_dir
        self.ca_cert = os.path.join(certs_dir, "ca.pem")
        self.ca_key = os.path.join(certs_dir, "ca.key")

    # -- lifecycle ------------------------------------------------------------

    def exists(self) -> bool:
        return os.path.isfile(self.ca_cert) and os.path.isfile(self.ca_key)

    def ensure(self) -> None:
        """Create the CA if missing."""
        if self.exists():
            return
        os.makedirs(self.dir, exist_ok=True)
        log.info("creating certificate authority in %s", self.dir)
        _run(["openssl", "genrsa", "-out", self.ca_key, "4096"])
        _run(["openssl", "req", "-x509", "-new", "-nodes",
              "-key", self.ca_key, "-sha256", "-days", "1825",
              "-subj", "/C=US/O=TAK-Revamp/CN=TAK-Revamp-CA",
              "-out", self.ca_cert])
        os.chmod(self.ca_key, 0o600)

    # -- signing --------------------------------------------------------------

    @staticmethod
    def normalize_csr(body: bytes) -> bytes:
        """Accept a CSR as PEM or as bare base64 DER (what ATAK sends)."""
        text = body.decode(errors="replace").strip()
        if "BEGIN CERTIFICATE REQUEST" in text:
            return text.encode()
        # bare base64 (possibly with whitespace/newlines) -> wrap as PEM
        b64 = re.sub(r"\s+", "", text)
        try:
            base64.b64decode(b64, validate=True)
        except Exception as e:  # noqa: BLE001
            raise CAError("request body is not a PEM or base64 CSR") from e
        wrapped = "\n".join(b64[i:i + 64] for i in range(0, len(b64), 64))
        return (f"-----BEGIN CERTIFICATE REQUEST-----\n{wrapped}\n"
                f"-----END CERTIFICATE REQUEST-----\n").encode()

    def sign_csr(self, csr_body: bytes, days: int = 365) -> str:
        """Sign a client CSR; returns the certificate as PEM."""
        if not self.exists():
            raise CAError("CA not initialised")
        csr_pem = self.normalize_csr(csr_body)
        with tempfile.TemporaryDirectory() as tmp:
            csr_path = os.path.join(tmp, "client.csr")
            ext_path = os.path.join(tmp, "client.ext")
            with open(csr_path, "wb") as f:
                f.write(csr_pem)
            with open(ext_path, "w") as f:
                f.write(CLIENT_EXT)
            # random serial: avoids writing a ca.srl file next to the CA,
            # which lives on a read-only mount inside the container
            serial = "0x" + secrets.token_hex(16)
            out = _run(["openssl", "x509", "-req", "-in", csr_path,
                        "-CA", self.ca_cert, "-CAkey", self.ca_key,
                        "-set_serial", serial, "-days", str(days), "-sha256",
                        "-extfile", ext_path])
        return out.decode()

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def pem_body(pem: str) -> str:
        """Strip PEM armour -> single-line base64 DER (what TAK clients expect
        in the signClient/v2 JSON response)."""
        lines = [ln.strip() for ln in pem.splitlines()
                 if ln.strip() and not ln.startswith("-----")]
        return "".join(lines)

    def ca_pem(self) -> str:
        with open(self.ca_cert) as f:
            return f.read()

    def ca_der(self) -> bytes:
        """CA certificate in DER form (what iOS profiles embed)."""
        return _run(["openssl", "x509", "-in", self.ca_cert,
                     "-outform", "der"])

    def make_client_p12(self, cn: str, password: str = "atakatak",
                        days: int = 365) -> bytes:
        """Generate a client keypair, sign it, and bundle key+cert+CA into a
        PKCS#12 (legacy algorithms so ATAK/iOS can read it). This is a
        ready-to-use client identity — the device needs no enrollment step."""
        if not self.exists():
            raise CAError("CA not initialised")
        with tempfile.TemporaryDirectory() as tmp:
            key = os.path.join(tmp, "client.key")
            csr = os.path.join(tmp, "client.csr")
            cert = os.path.join(tmp, "client.pem")
            p12 = os.path.join(tmp, "client.p12")
            _run(["openssl", "genrsa", "-out", key, "2048"])
            _run(["openssl", "req", "-new", "-key", key,
                  "-subj", f"/C=US/O=TAK-Revamp/CN={cn}", "-out", csr])
            with open(csr, "rb") as f:
                cert_pem = self.sign_csr(f.read(), days=days)
            with open(cert, "w") as f:
                f.write(cert_pem)
            # Classic PKCS12 format so modern ATAK can open it (RC2-40 cert +
            # SHA1 MAC, matching the truststore and what TAK Server emits).
            # ATAK v5.6 silently rejects 3DES/AES-256 p12s. RC2 needs -legacy.
            base = ["openssl", "pkcs12", "-export", "-inkey", key,
                    "-in", cert, "-certfile", self.ca_cert,
                    "-name", cn, "-out", p12, "-passout", f"pass:{password}"]
            for extra in (["-legacy", "-certpbe", "PBE-SHA1-RC2-40", "-keypbe",
                           "PBE-SHA1-3DES", "-macalg", "sha1"],
                          ["-certpbe", "PBE-SHA1-3DES", "-keypbe",
                           "PBE-SHA1-3DES", "-macalg", "sha1"],
                          ["-legacy"], []):
                try:
                    _run(base + extra)
                    break
                except CAError:
                    continue
            with open(p12, "rb") as f:
                return f.read()

    def verify(self, cert_pem: str) -> bool:
        """Check a certificate chains to this CA (used by tests)."""
        with tempfile.NamedTemporaryFile(suffix=".pem") as f:
            f.write(cert_pem.encode())
            f.flush()
            try:
                _run(["openssl", "verify", "-CAfile", self.ca_cert, f.name])
                return True
            except CAError:
                return False


def subject_cn(cert_pem: str) -> Optional[str]:
    """Extract CN from a certificate (for mTLS identification later)."""
    try:
        out = _run(["openssl", "x509", "-noout", "-subject",
                    "-nameopt", "sep_multiline"], cert_pem.encode())
    except CAError:
        return None
    m = re.search(r"CN=(.+)", out.decode())
    return m.group(1).strip() if m else None
