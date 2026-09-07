"""Marti API conformance tests: lock the enrollment contract that real
ATAK/iTAK clients depend on (signClient/v2 JSON shape, tls/config XML,
401 handling, single-use token exhaustion) so client compatibility can't
silently regress. Requires the ``openssl`` CLI."""

import base64
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.ca import CertificateAuthority  # noqa: E402
from takcore.enrollment import EnrollmentService, make_marti_handler  # noqa: E402
from takcore.store import Store  # noqa: E402


def _make_csr(tmp: str) -> bytes:
    key = f"{tmp}/k.pem"
    subprocess.run(["openssl", "genrsa", "-out", key, "2048"],
                   check=True, capture_output=True)
    out = subprocess.run(
        ["openssl", "req", "-new", "-key", key, "-subj", "/CN=conform-test"],
        check=True, capture_output=True)
    return out.stdout


class TestMartiConformance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ca = CertificateAuthority(cls.tmp.name)
        cls.ca.ensure()
        cls.store = Store(":memory:")
        cls.svc = EnrollmentService(cls.store, cls.ca)
        # plain-HTTP server is fine: we're testing routes, not TLS
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        make_marti_handler(cls.svc))
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.store.close()
        cls.tmp.cleanup()

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_tls_config_is_xml_with_name_entries(self):
        c = self._conn()
        c.request("GET", "/Marti/api/tls/config")
        r = c.getresponse()
        body = r.read().decode()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "application/xml")
        self.assertIn("certificateConfig", body)
        self.assertIn('nameEntry name="O"', body)

    def test_sign_client_v2_contract(self):
        tok = self.svc.create_token(username="conform")
        auth = base64.b64encode(
            f"conform:{tok['token']}".encode()).decode()
        with tempfile.TemporaryDirectory() as t:
            csr = _make_csr(t)
        # ATAK sends bare base64 DER, no PEM armour, no padding concerns
        der_b64 = "".join(l for l in csr.decode().splitlines()
                          if not l.startswith("-----"))
        c = self._conn()
        c.request("POST", "/Marti/api/tls/signClient/v2", body=der_b64,
                  headers={"Authorization": f"Basic {auth}"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        data = json.loads(r.read())
        # Contract both ATAK and iTAK parse. iTAK requires the ca0+ca1
        # pair to build its truststore (it aborts enrollment with only
        # ca0), so we duplicate the CA into ca1 like TAK Server / OTS do.
        self.assertEqual(set(data.keys()), {"signedCert", "ca0", "ca1"})
        self.assertEqual(data["ca0"], data["ca1"])
        for v in data.values():
            self.assertNotIn("BEGIN", v)   # bare base64, not PEM
            base64.b64decode(v, validate=True)  # must decode cleanly

    def test_sign_client_honors_text_plain_accept(self):
        # iTAK sends Accept: text/plain and wants the JSON body served
        # with that content type; ATAK gets application/json as before.
        tok = self.svc.create_token(username="acc")
        auth = base64.b64encode(f"acc:{tok['token']}".encode()).decode()
        with tempfile.TemporaryDirectory() as t:
            csr = _make_csr(t)
        der_b64 = "".join(l for l in csr.decode().splitlines()
                          if not l.startswith("-----"))
        c = self._conn()
        c.request("POST", "/Marti/api/tls/signClient/v2", body=der_b64,
                  headers={"Authorization": f"Basic {auth}",
                           "Accept": "text/plain"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("text/plain", r.getheader("Content-Type", ""))
        json.loads(r.read())  # body is still JSON

    def test_sign_client_requires_auth(self):
        c = self._conn()
        c.request("POST", "/Marti/api/tls/signClient/v2", body="x")
        r = c.getresponse()
        self.assertEqual(r.status, 401)
        self.assertIn("Basic", r.getheader("WWW-Authenticate", ""))
        r.read()

    def test_single_use_token_exhausts(self):
        tok = self.svc.create_token(username="once", max_uses=1)
        self.assertTrue(self.svc.verify("once", tok["token"]))
        self.store.consume_enrollment("once")
        self.assertFalse(self.svc.verify("once", tok["token"]))


if __name__ == "__main__":
    unittest.main()
