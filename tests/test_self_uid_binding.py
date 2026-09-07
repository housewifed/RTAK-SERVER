import unittest

from takcore.cot import CotEvent
from takcore.hub import Hub, TakSession
from takcore.store import Store


class FakeWriter:
    def is_closing(self):
        return False
    def write(self, data):
        pass


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

    def test_spoofed_self_position_not_stored(self):
        self.hub.publish(_pos("DEV-A"), self.a)
        self.b.uid = None
        self.hub.publish(_pos("DEV-A"), self.b)  # spoof from B, must be dropped
        # DEV-A still owned by A; store has DEV-A once, from A
        self.assertIs(self.hub._uid_owner["DEV-A"], self.a)


if __name__ == "__main__":
    unittest.main()
