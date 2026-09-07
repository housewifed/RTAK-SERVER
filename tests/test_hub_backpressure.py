import queue
import unittest

from takcore.hub import Hub
from takcore.store import Store


class TestHubBackpressure(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(Store(":memory:"))

    def test_slow_subscriber_is_dropped_and_told_to_close(self):
        q = self.hub.subscribe_web()
        # fill to capacity so the next push overflows
        for _ in range(q.maxsize):
            q.put_nowait("stale")
        self.hub._push_web({"kind": "position", "uid": "A"})
        # dropped from the live set...
        self.assertNotIn(q, self.hub._web_subs)
        # ...and the handler's next get returns the CLOSE sentinel promptly
        self.assertIs(q.get_nowait(), Hub.CLOSE)

    def test_healthy_subscriber_receives_json(self):
        q = self.hub.subscribe_web()
        self.hub._push_web({"kind": "position", "uid": "A"})
        self.assertIn("\"uid\": \"A\"", q.get_nowait())


if __name__ == "__main__":
    unittest.main()
