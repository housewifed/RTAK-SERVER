"""Unit tests for the CA and enrollment service (requires openssl CLI)."""

import base64
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.ca import CertificateAuthority, subject_cn  # noqa: E402
from takcore.enrollment import EnrollmentService, hash_token  # noqa: E402
from takcore.store import Store  # noqa: E402


def make_csr(cn: str, tmp: str) -> bytes:
    key = os.path.join(tmp, "k.pem")
    csr = os.path.join(tmp, "c.csr")
    subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048",
                    "-nodes", "-keyout", key, "-subj", f"/CN={cn}",
                    "-out", csr], capture_output=True, check=True)
    with open(csr, "rb") as f:
        return f.read()


class TestCA(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ca = CertificateAuthority(os.path.join(cls.tmp.name, "certs"))
        cls.ca.ensure()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_ca_created(self):
        self.assertTrue(self.ca.exists())

    def test_sign_pem_csr(self):
        csr = make_csr("VIPER-9", self.tmp.name)
        cert = self.ca.sign_csr(csr)
        self.assertIn("BEGIN CERTIFICATE", cert)
        self.assertTrue(self.ca.verify(cert))
        self.assertEqual(subject_cn(cert), "VIPER-9")

    def test_sign_bare_base64_csr(self):
        """ATAK sends the CSR as bare base64 DER, no PEM armour."""
        csr_pem = make_csr("atak-style", self.tmp.name)
        body = "".join(ln for ln in csr_pem.decode().splitlines()
                       if not ln.startswith("-----"))
        cert = self.ca.sign_csr(body.encode())
        self.assertTrue(self.ca.verify(cert))

    def test_pem_body_is_valid_b64_der(self):
        csr = make_csr("x", self.tmp.name)
        cert = self.ca.sign_csr(csr)
        der = base64.b64decode(self.ca.pem_body(cert))
        self.assertEqual(der[0:1], b"\x30")  # ASN.1 SEQUENCE


class TestEnrollmentService(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "t.db"))
        self.ca = CertificateAuthority(os.path.join(self.tmp.name, "certs"))
        self.ca.ensure()
        self.svc = EnrollmentService(self.store, self.ca)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_token_roundtrip(self):
        t = self.svc.create_token(callsign="VIPER-2")
        self.assertTrue(self.svc.verify(t["username"], t["token"]))
        self.assertFalse(self.svc.verify(t["username"], "wrong"))
        self.assertFalse(self.svc.verify("nobody", t["token"]))

    def test_single_use(self):
        t = self.svc.create_token(max_uses=1)
        self.assertTrue(self.svc.verify(t["username"], t["token"]))
        self.svc.sign(t["username"], make_csr("d", self.tmp.name))
        self.assertFalse(self.svc.verify(t["username"], t["token"]))

    def test_default_token_is_reusable(self):
        # default (no max_uses) stays valid across the multiple requests a real
        # enrollment makes (ATAK re-fetch, iTAK config+signClient, retries)
        t = self.svc.create_token()
        self.svc.sign(t["username"], make_csr("r1", self.tmp.name))
        self.assertTrue(self.svc.verify(t["username"], t["token"]))
        self.svc.sign(t["username"], make_csr("r2", self.tmp.name))
        self.assertTrue(self.svc.verify(t["username"], t["token"]))

    def test_expiry(self):
        t = self.svc.create_token(expires_hours=1)
        user = self.store.get_enroll_user(t["username"])
        # rewind the expiry into the past
        self.store.create_enroll_user(t["username"], user["token_hash"],
                                      None, time.time() - 10, None)
        self.assertFalse(self.svc.verify(t["username"], t["token"]))

    def test_sign_response_shape(self):
        t = self.svc.create_token()
        result = self.svc.sign(t["username"], make_csr("d2", self.tmp.name))
        self.assertIn("signedCert", result)
        self.assertIn("ca0", result)
        base64.b64decode(result["signedCert"])
        base64.b64decode(result["ca0"])

    def test_token_hash_stable(self):
        self.assertEqual(hash_token("abc"), hash_token("abc"))
        self.assertNotEqual(hash_token("abc"), hash_token("abd"))


if __name__ == "__main__":
    unittest.main()
