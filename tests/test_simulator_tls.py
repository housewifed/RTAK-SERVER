import ssl
import unittest

from tests.simulate_atak import _make_ssl_context


class _Args:
    tls = True
    cafile = None
    cert = None
    key = None


class TestSimulatorTls(unittest.TestCase):
    def test_context_created_when_tls_flag_set(self):
        ctx = _make_ssl_context(_Args())
        self.assertIsInstance(ctx, ssl.SSLContext)
        # self-signed lab certs: verification off unless a cafile is given
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(ctx.check_hostname)

    def test_no_context_when_tls_disabled(self):
        args = _Args()
        args.tls = False
        self.assertIsNone(_make_ssl_context(args))
