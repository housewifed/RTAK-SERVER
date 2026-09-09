import unittest

from takcore.cot import CotEvent
from takcore.hub import Hub, TakSession
from takcore.store import Store


class FakeWriter:
    def __init__(self):
        self.closed = False
    def is_closing(self):
        return self.closed
    def write(self, data):
        pass
    def close(self):
        self.closed = True


def _pos(uid, lat=38.9, lon=-77.0):
    return CotEvent(uid=uid, type="a-f-G-U-C", lat=lat, lon=lon,
                    callsign=uid, raw=f"<event uid='{uid}'/>".encode())


class TestSelfUidBinding(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(Store(":memory:"))
        self.a = TakSession(FakeWriter(), "10.0.0.1:1000")
        self.b = TakSession(FakeWriter(), "10.0.0.2:2000")
        self.hub.register(self.a)
        self.hub.register(self.b)

    def test_first_self_position_binds(self):
        self.assertTrue(self.hub._check_self_uid(_pos("DEV-A"), self.a))
        self.assertIs(self.hub._uid_owner["DEV-A"], self.a)

    def test_foreign_session_self_position_rejected(self):
        self.hub._check_self_uid(_pos("DEV-A"), self.a)   # A owns DEV-A
        self.assertFalse(self.hub._check_self_uid(_pos("DEV-A"), self.b))

    def test_release_on_disconnect_allows_rebind(self):
        self.hub._check_self_uid(_pos("DEV-A"), self.a)
        self.hub.unregister(self.a)
        self.assertTrue(self.hub._check_self_uid(_pos("DEV-A"), self.b))

    def test_marker_with_foreign_uid_passes(self):
        self.hub._check_self_uid(_pos("DEV-A"), self.a)   # A's own uid
        self.a.uid = "DEV-A"
        # A emits a marker carrying a different uid -> not a self-position
        self.assertTrue(self.hub._check_self_uid(_pos("MARKER-9"), self.a))

    # -- reconnects from a new address (mobile roaming) --------------------

    def test_same_certificate_may_take_over_its_own_uid(self):
        """A phone moving Wi-Fi -> cellular reconnects from a new address while
        the old socket is still a zombie ESTAB. Identity is proven by the client
        certificate, so the same CN must be allowed to reclaim its own uid -
        otherwise the device is locked out of reporting until the dead socket is
        reaped, which can take hours."""
        self.a.cn = "Targa2"
        self.b.cn = "Targa2"
        self.assertTrue(self.hub._check_self_uid(_pos("DEV-A"), self.a))
        self.assertTrue(self.hub._check_self_uid(_pos("DEV-A"), self.b))
        self.assertIs(self.hub._uid_owner["DEV-A"], self.b)

    def test_a_different_certificate_is_still_rejected(self):
        self.a.cn = "Targa2"
        self.b.cn = "SomeoneElse"
        self.hub._check_self_uid(_pos("DEV-A"), self.a)
        self.assertFalse(self.hub._check_self_uid(_pos("DEV-A"), self.b))
        self.assertIs(self.hub._uid_owner["DEV-A"], self.a)

    def test_without_certificates_the_strict_rule_still_applies(self):
        """No mTLS (lab mode): there is no proven identity to compare, so a
        second session must not be able to claim a bound uid."""
        self.assertIsNone(self.a.cn)
        self.assertIsNone(self.b.cn)
        self.hub._check_self_uid(_pos("DEV-A"), self.a)
        self.assertFalse(self.hub._check_self_uid(_pos("DEV-A"), self.b))

    def test_takeover_closes_the_stale_session(self):
        self.a.cn = self.b.cn = "Targa2"
        self.hub._check_self_uid(_pos("DEV-A"), self.a)
        self.hub._check_self_uid(_pos("DEV-A"), self.b)
        self.assertTrue(self.a.writer.closed, "the zombie session should be closed")
        self.assertFalse(self.b.writer.closed)

    def test_position_from_the_new_session_is_stored(self):
        self.a.cn = self.b.cn = "Targa2"
        self.hub.publish(_pos("DEV-A", lat=38.9, lon=-77.0), self.a)
        self.hub.publish(_pos("DEV-A", lat=43.7, lon=-79.4), self.b)
        d = self.hub.store.device("DEV-A")
        self.assertAlmostEqual(d["lat"], 43.7, places=3)

    def test_spoofed_self_position_not_stored(self):
        self.hub.publish(_pos("DEV-A"), self.a)
        self.b.uid = None
        self.hub.publish(_pos("DEV-A"), self.b)  # spoof from B, must be dropped
        # DEV-A still owned by A; store has DEV-A once, from A
        self.assertIs(self.hub._uid_owner["DEV-A"], self.a)


if __name__ == "__main__":
    unittest.main()
