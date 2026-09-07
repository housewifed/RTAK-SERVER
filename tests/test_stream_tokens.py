import time
import unittest

from takcore.stream_tokens import make_ticket, verify_ticket

SECRET = b"unit-test-secret"


class TestStreamTokens(unittest.TestCase):
    def test_valid_ticket_round_trips(self):
        tok = make_ticket(SECRET, "live/DEV-1", ttl=60)
        self.assertTrue(verify_ticket(SECRET, "live/DEV-1", tok))

    def test_wrong_path_rejected(self):
        tok = make_ticket(SECRET, "live/DEV-1", ttl=60)
        self.assertFalse(verify_ticket(SECRET, "live/DEV-2", tok))

    def test_wrong_secret_rejected(self):
        tok = make_ticket(SECRET, "live/DEV-1", ttl=60)
        self.assertFalse(verify_ticket(b"other", "live/DEV-1", tok))

    def test_expired_ticket_rejected(self):
        tok = make_ticket(SECRET, "live/DEV-1", ttl=-1)
        self.assertFalse(verify_ticket(SECRET, "live/DEV-1", tok))

    def test_garbage_rejected(self):
        self.assertFalse(verify_ticket(SECRET, "live/DEV-1", "not-a-token"))
        self.assertFalse(verify_ticket(SECRET, "live/DEV-1", ""))


from takcore.mediamtx import MediaMTX, StreamRegistry
from takcore.store import Store


class TestWebrtcAuthorize(unittest.TestCase):
    def setUp(self):
        self.reg = StreamRegistry(Store(":memory:"),
                                  MediaMTX("http://127.0.0.1:1"),
                                  token_secret=b"authz-secret")

    def test_rtsp_read_allowed_without_ticket(self):
        self.assertTrue(self.reg.authorize(
            {"action": "read", "path": "live/DEV", "protocol": "rtsp"}))

    def test_webrtc_read_denied_without_ticket(self):
        self.assertFalse(self.reg.authorize(
            {"action": "read", "path": "live/DEV", "protocol": "webrtc",
             "query": ""}))

    def test_webrtc_read_allowed_with_ticket(self):
        t = self.reg.make_ticket("live/DEV")["token"]
        self.assertTrue(self.reg.authorize(
            {"action": "read", "path": "live/DEV", "protocol": "webrtc",
             "query": f"token={t}"}))

    def test_webrtc_ticket_bound_to_path(self):
        t = self.reg.make_ticket("live/DEV")["token"]
        self.assertFalse(self.reg.authorize(
            {"action": "read", "path": "live/OTHER", "protocol": "webrtc",
             "query": f"token={t}"}))


if __name__ == "__main__":
    unittest.main()
