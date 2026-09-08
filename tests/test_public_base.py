"""The address the enrollment QR codes must encode.

The QRs used to be built from location.port in the browser, which produced
http://<host>:8080/... for an admin sitting on https://<domain> - a URL the
phone could not reach. The server now computes the phone-facing base itself.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.webserver import https_enabled, public_web_base  # noqa: E402


class TestHttpsEnabled(unittest.TestCase):
    def test_a_real_domain_means_https(self):
        self.assertTrue(https_enabled({"TAK_DOMAIN": "rtak.example.com"}))

    def test_localhost_is_not_https(self):
        self.assertFalse(https_enabled({"TAK_DOMAIN": "localhost"}))

    def test_missing_domain_is_not_https(self):
        self.assertFalse(https_enabled({}))
        self.assertFalse(https_enabled({"TAK_DOMAIN": ""}))


class TestPublicWebBase(unittest.TestCase):
    def test_https_drops_the_default_port(self):
        self.assertEqual(
            public_web_base({"TAK_DOMAIN": "rtak.example.com",
                             "SERVER_HOST": "rtak.example.com",
                             "HTTP_PORT": "8081"}),
            "https://rtak.example.com")

    def test_https_keeps_a_non_standard_port(self):
        self.assertEqual(
            public_web_base({"TAK_DOMAIN": "rtak.example.com",
                             "CADDY_HTTPS_PORT": "8443"}),
            "https://rtak.example.com:8443")

    def test_https_ignores_the_takcore_port(self):
        """Caddy fronts the server, so HTTP_PORT must not leak into the URL -
        this is the bug the QR codes had."""
        base = public_web_base({"TAK_DOMAIN": "rtak.example.com",
                                "HTTP_PORT": "8081"})
        self.assertNotIn("8081", base)

    def test_plain_http_uses_server_host_and_http_port(self):
        self.assertEqual(
            public_web_base({"SERVER_HOST": "192.168.2.101",
                             "HTTP_PORT": "8081"}),
            "http://192.168.2.101:8081")

    def test_localhost_domain_falls_back_to_plain_http(self):
        self.assertEqual(
            public_web_base({"TAK_DOMAIN": "localhost",
                             "SERVER_HOST": "192.168.2.101",
                             "HTTP_PORT": "8081"}),
            "http://192.168.2.101:8081")

    def test_http_port_defaults_to_8080(self):
        self.assertEqual(public_web_base({"SERVER_HOST": "10.0.0.5"}),
                         "http://10.0.0.5:8080")

    def test_empty_when_nothing_is_configured(self):
        self.assertEqual(public_web_base({}), "")

    def test_empty_port_values_fall_back_to_defaults(self):
        self.assertEqual(public_web_base({"SERVER_HOST": "10.0.0.5",
                                          "HTTP_PORT": ""}),
                         "http://10.0.0.5:8080")
        self.assertEqual(public_web_base({"TAK_DOMAIN": "d.example.com",
                                          "CADDY_HTTPS_PORT": ""}),
                         "https://d.example.com")


if __name__ == "__main__":
    unittest.main()
