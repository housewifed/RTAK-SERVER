import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(__file__), "..",
                      "scripts", "make-certs.sh")


class TestMakeCertsImport(unittest.TestCase):
    def test_import_mode_installs_external_cert(self):
        with tempfile.TemporaryDirectory() as tmp:
            ext_key = f"{tmp}/privkey.pem"
            ext_crt = f"{tmp}/fullchain.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048",
                 "-nodes", "-keyout", ext_key, "-out", ext_crt,
                 "-days", "2", "-subj", "/CN=example.test"],
                check=True, capture_output=True)
            env = dict(os.environ, CERTS_DIR=f"{tmp}/certs")
            r = subprocess.run(
                ["bash", SCRIPT, "--import", ext_crt, ext_key],
                env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            certs = f"{tmp}/certs"
            for f in ("server.pem", "server.key", "ca.pem", "ca.key"):
                self.assertTrue(os.path.isfile(f"{certs}/{f}"), f)
            subj = subprocess.run(
                ["openssl", "x509", "-in", f"{certs}/server.pem",
                 "-noout", "-subject"], capture_output=True, text=True)
            self.assertIn("example.test", subj.stdout)

    def test_import_mode_ca_has_v3_extensions(self):
        # The client-signing CA minted in --import mode must carry the same
        # v3 extensions as the self-signed flow's CA: Android/ATAK's
        # Conscrypt TLS stack rejects a CA cert without basicConstraints
        # CA:TRUE and keyCertSign.
        with tempfile.TemporaryDirectory() as tmp:
            ext_key = f"{tmp}/privkey.pem"
            ext_crt = f"{tmp}/fullchain.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048",
                 "-nodes", "-keyout", ext_key, "-out", ext_crt,
                 "-days", "2", "-subj", "/CN=example.test"],
                check=True, capture_output=True)
            env = dict(os.environ, CERTS_DIR=f"{tmp}/certs")
            r = subprocess.run(
                ["bash", SCRIPT, "--import", ext_crt, ext_key],
                env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            certs = f"{tmp}/certs"
            text = subprocess.run(
                ["openssl", "x509", "-in", f"{certs}/ca.pem",
                 "-noout", "-text"], capture_output=True, text=True)
            self.assertIn("CA:TRUE", text.stdout)
            self.assertIn("Certificate Sign", text.stdout)

    def test_import_mode_rejects_swapped_arguments(self):
        # --import fullchain.pem privkey.pem is the documented order; passing
        # them swapped (privkey.pem fullchain.pem) must fail validation
        # rather than silently copy a mismatched/garbage pair into place.
        with tempfile.TemporaryDirectory() as tmp:
            ext_key = f"{tmp}/privkey.pem"
            ext_crt = f"{tmp}/fullchain.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048",
                 "-nodes", "-keyout", ext_key, "-out", ext_crt,
                 "-days", "2", "-subj", "/CN=example.test"],
                check=True, capture_output=True)
            env = dict(os.environ, CERTS_DIR=f"{tmp}/certs")
            r = subprocess.run(
                ["bash", SCRIPT, "--import", ext_key, ext_crt],
                env=env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse(
                os.path.isfile(f"{tmp}/certs/server.pem"),
                "server.pem should not exist after a rejected import")

    def test_import_mode_rejects_mismatched_cert_and_key(self):
        # Each file is individually a valid cert / valid key, but they don't
        # pair: importing must abort rather than install a server.pem/
        # server.key pair that ssl.load_cert_chain will reject at restart.
        with tempfile.TemporaryDirectory() as tmp:
            ext_key = f"{tmp}/privkey.pem"
            ext_crt = f"{tmp}/fullchain.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048",
                 "-nodes", "-keyout", ext_key, "-out", ext_crt,
                 "-days", "2", "-subj", "/CN=example.test"],
                check=True, capture_output=True)
            other_key = f"{tmp}/other-privkey.pem"
            other_crt = f"{tmp}/other-fullchain.pem"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048",
                 "-nodes", "-keyout", other_key, "-out", other_crt,
                 "-days", "2", "-subj", "/CN=other.test"],
                check=True, capture_output=True)
            env = dict(os.environ, CERTS_DIR=f"{tmp}/certs")
            r = subprocess.run(
                ["bash", SCRIPT, "--import", ext_crt, other_key],
                env=env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertFalse(
                os.path.isfile(f"{tmp}/certs/server.pem"),
                "server.pem should not exist after a rejected import")
