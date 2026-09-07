import unittest

from takcore.cot import CotEvent
from takcore.hub import Hub
from takcore.store import Store


def _pos(uid="DEV-1"):
    return CotEvent(uid=uid, type="a-f-G-U-C", lat=38.9, lon=-77.0,
                    callsign="Alpha", raw=b"<event/>")


def _chat():
    ev = CotEvent(uid="GeoChat.x", type="b-t-f", raw=b"<event/>")
    ev.chat_message = "hello"
    ev.chat_sender = "Alpha"
    ev.chat_room = "All Chat Rooms"
    return ev


def _emergency():
    return CotEvent(uid="DEV-1", type="b-a-o-tbl", callsign="Alpha",
                    lat=38.9, lon=-77.0, emergency_type="911 Alert",
                    raw=b"<event/>")


class TestHubDispatch(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(Store(":memory:"))

    def test_position_routes_to_store(self):
        self.hub.publish(_pos(), origin=None)
        self.assertIsNotNone(self.hub.store.device("DEV-1"))

    def test_chat_routes_to_store(self):
        self.hub.publish(_chat(), origin=None)
        self.assertEqual(len(self.hub.store.chat_history()), 1)

    def test_emergency_routes_to_alerts(self):
        self.hub.publish(_emergency(), origin=None)
        alerts = self.hub.store.active_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["uid"], "DEV-1")


if __name__ == "__main__":
    unittest.main()
