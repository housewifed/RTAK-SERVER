"""Verify the ATAK enrollment data package structure."""

import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.datapackage import build_enrollment_package  # noqa: E402


class TestDataPackage(unittest.TestCase):
    def setUp(self):
        self.pkg = build_enrollment_package(
            "192.168.2.190", b"FAKEP12BYTES", callsign="VIPER-2")
        self.zf = zipfile.ZipFile(io.BytesIO(self.pkg))

    def test_zip_entries(self):
        names = set(self.zf.namelist())
        self.assertIn("MANIFEST/MANIFEST.xml", names)
        self.assertIn("certs/config.pref", names)
        self.assertIn("certs/caCert.p12", names)

    def test_ca_bytes_preserved(self):
        self.assertEqual(self.zf.read("certs/caCert.p12"), b"FAKEP12BYTES")

    def test_config_has_connect_string_and_enrollment(self):
        cfg = self.zf.read("certs/config.pref").decode()
        self.assertIn("192.168.2.190:8089:ssl", cfg)
        self.assertIn("enrollForCertificateWithTrust0", cfg)
        self.assertIn("useAuth0", cfg)
        self.assertIn("cert/caCert.p12", cfg)   # runtime path, not certs/
        self.assertIn("VIPER-2", cfg)

    def test_manifest_has_uid_and_contents(self):
        man = self.zf.read("MANIFEST/MANIFEST.xml").decode()
        self.assertIn("uid", man)
        self.assertIn("certs/config.pref", man)
        self.assertIn("certs/caCert.p12", man)

    def test_manifest_uid_is_unique(self):
        p2 = build_enrollment_package("h", b"x")
        m1 = zipfile.ZipFile(io.BytesIO(self.pkg)).read("MANIFEST/MANIFEST.xml")
        m2 = zipfile.ZipFile(io.BytesIO(p2)).read("MANIFEST/MANIFEST.xml")
        self.assertNotEqual(m1, m2)

    def test_no_callsign_is_valid(self):
        pkg = build_enrollment_package("h", b"x")
        cfg = zipfile.ZipFile(io.BytesIO(pkg)).read("certs/config.pref").decode()
        self.assertNotIn("locationCallsign", cfg)


class TestSoftCertPackage(unittest.TestCase):
    def setUp(self):
        from takcore.datapackage import build_softcert_package
        self.pkg = build_softcert_package(
            "192.168.2.190", b"CABYTES", b"CLIENTP12", callsign="Anderson")
        self.zf = zipfile.ZipFile(io.BytesIO(self.pkg))

    def test_has_client_cert(self):
        names = set(self.zf.namelist())
        self.assertIn("certs/clientCert.p12", names)
        self.assertIn("certs/caCert.p12", names)
        self.assertEqual(self.zf.read("certs/clientCert.p12"), b"CLIENTP12")

    def test_config_uses_client_cert_not_enrollment(self):
        cfg = self.zf.read("certs/config.pref").decode()
        self.assertIn("certificateLocation0", cfg)
        self.assertIn("cert/clientCert.p12", cfg)
        self.assertIn("clientPassword0", cfg)
        self.assertIn("caLocation0", cfg)
        # soft-cert must NOT ask ATAK to run enrollment
        self.assertNotIn("enrollForCertificateWithTrust", cfg)
        self.assertIn("192.168.2.190:8089:ssl", cfg)

    def test_manifest_lists_client_cert(self):
        man = self.zf.read("MANIFEST/MANIFEST.xml").decode()
        self.assertIn("certs/clientCert.p12", man)


if __name__ == "__main__":
    unittest.main()
