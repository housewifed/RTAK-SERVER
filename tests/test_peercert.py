import unittest

from takcore.taklistener import cn_from_peercert


class TestPeerCertCN(unittest.TestCase):
    def test_extracts_cn(self):
        cert = {"subject": ((("countryName", "US"),),
                            (("commonName", "VIPER-2"),))}
        self.assertEqual(cn_from_peercert(cert), "VIPER-2")

    def test_none_when_no_cert(self):
        self.assertIsNone(cn_from_peercert(None))

    def test_none_when_no_cn(self):
        cert = {"subject": ((("organizationName", "TAK-Revamp"),),)}
        self.assertIsNone(cn_from_peercert(cert))


if __name__ == "__main__":
    unittest.main()
