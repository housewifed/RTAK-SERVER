"""Tests for the iOS trust profile (.mobileconfig) that lets iPhones trust
the self-signed CA so iTAK's QR enrollment can complete its TLS handshake."""

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.ca import CertificateAuthority  # noqa: E402
from takcore.enrollment import EnrollmentService  # noqa: E402
from takcore.store import Store  # noqa: E402


class TestIosTrustProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ca = CertificateAuthority(os.path.join(cls.tmp.name, "certs"))
        cls.ca.ensure()
        cls.store = Store(os.path.join(cls.tmp.name, "t.db"))
        cls.svc = EnrollmentService(cls.store, cls.ca)

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_profile_is_valid_plist_with_ca_payload(self):
        data = self.svc.ios_trust_profile()
        self.assertIsNotNone(data)
        prof = plistlib.loads(data)
        self.assertEqual(prof["PayloadType"], "Configuration")
        payloads = prof["PayloadContent"]
        self.assertEqual(len(payloads), 1)
        cert = payloads[0]
        self.assertEqual(cert["PayloadType"], "com.apple.security.root")
        # payload must be the CA certificate in DER form
        der = subprocess.run(
            ["openssl", "x509", "-in", self.ca.ca_cert, "-outform", "der"],
            capture_output=True, check=True).stdout
        self.assertEqual(bytes(cert["PayloadContent"]), der)

    def test_profile_is_stable_across_calls(self):
        # same CA -> identical profile (stable UUIDs), so reinstalling
        # updates the existing profile instead of stacking duplicates
        self.assertEqual(self.svc.ios_trust_profile(),
                         self.svc.ios_trust_profile())

    def test_profile_none_without_ca(self):
        empty = CertificateAuthority(os.path.join(self.tmp.name, "nocerts"))
        svc = EnrollmentService(self.store, empty)
        self.assertIsNone(svc.ios_trust_profile())


if __name__ == "__main__":
    unittest.main()
