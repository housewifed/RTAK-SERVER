"""make-certs.sh must be able to put several SANs on the server cert so one
cert is valid for the DDNS name (rtak.ddns.net) AND the LAN IP — the DDNS name
for remote clients, the IP for on-LAN clients when the router won't hairpin.
"""

import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..",
                      "scripts", "make-certs.sh")


def _san(cert_path):
    return subprocess.run(
        ["openssl", "x509", "-in", cert_path, "-noout", "-ext",
         "subjectAltName"], capture_output=True, text=True).stdout


class TestMakeCertsSAN(unittest.TestCase):
    def test_multi_san_covers_hostname_and_ip(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CERTS_DIR=f"{tmp}/certs")
            r = subprocess.run(["bash", SCRIPT, "rtak.ddns.net", "192.168.2.190"],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            san = _san(f"{tmp}/certs/server.pem")
            self.assertIn("DNS:rtak.ddns.net", san)
            self.assertIn("IP Address:192.168.2.190", san)
            self.assertIn("DNS:localhost", san)        # convenience SANs kept
            self.assertIn("IP Address:127.0.0.1", san)
            subj = subprocess.run(
                ["openssl", "x509", "-in", f"{tmp}/certs/server.pem",
                 "-noout", "-subject"], capture_output=True, text=True)
            self.assertIn("rtak.ddns.net", subj.stdout)   # CN = first host

    def test_single_hostname_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CERTS_DIR=f"{tmp}/certs")
            r = subprocess.run(["bash", SCRIPT, "rtak.ddns.net"],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("DNS:rtak.ddns.net", _san(f"{tmp}/certs/server.pem"))

    def test_single_ip_still_maps_to_ip_san(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, CERTS_DIR=f"{tmp}/certs")
            r = subprocess.run(["bash", SCRIPT, "192.168.2.190"],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("IP Address:192.168.2.190",
                          _san(f"{tmp}/certs/server.pem"))


if __name__ == "__main__":
    unittest.main()
