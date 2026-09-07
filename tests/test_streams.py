"""Unit tests for the stream registry and MediaMTX auth policy."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.mediamtx import (  # noqa: E402
    MediaMTX, StreamRegistry, sanitize_path,
)
from takcore.store import Store  # noqa: E402


class FakeMTX(MediaMTX):
    """Records calls instead of hitting the network."""

    def __init__(self):
        super().__init__("http://fake:9997")
        self.paths = {}
        self.records = {}

    def add_proxy_path(self, name, source):
        self.paths[name] = source
        return True

    def remove_path(self, name):
        self.paths.pop(name, None)
        return True

    def ready_paths(self):
        return {name: True for name in self.paths}

    def record_paths(self):
        return dict(self.records)

    def set_record(self, name, enabled):
        self.records[name] = bool(enabled)
        return True


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.store = Store(self.db.name)
        self.mtx = FakeMTX()
        self.reg = StreamRegistry(self.store, self.mtx)

    def tearDown(self):
        self.store.close()
        os.unlink(self.db.name)

    def test_sanitize(self):
        self.assertEqual(sanitize_path("gate north #2!"), "gate-north--2")
        self.assertEqual(sanitize_path("ANDROID-abc_123"), "ANDROID-abc_123")

    def test_publish_auth_autoregisters_device_stream(self):
        ok = self.reg.authorize({"action": "publish", "path": "live/ANDROID-x1",
                                 "ip": "10.0.0.9"})
        self.assertTrue(ok)
        rows = self.reg.streams_for_device("ANDROID-x1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "live/ANDROID-x1")
        self.assertEqual(rows[0]["kind"], "device")

    def test_read_always_allowed(self):
        self.assertTrue(self.reg.authorize({"action": "read", "path": "live/x"}))

    def test_publish_token_enforced(self):
        reg = StreamRegistry(self.store, self.mtx, publish_token="s3cret")
        deny = reg.authorize({"action": "publish", "path": "live/a",
                              "password": "wrong", "query": ""})
        self.assertFalse(deny)
        by_pass = reg.authorize({"action": "publish", "path": "live/a",
                                 "password": "s3cret", "query": ""})
        self.assertTrue(by_pass)
        by_query = reg.authorize({"action": "publish", "path": "live/b",
                                  "password": "", "query": "token=s3cret"})
        self.assertTrue(by_query)

    def test_camera_registration_pushes_proxy(self):
        result = self.reg.register_camera("Gate North", "rtsp://10.0.0.5/cam")
        self.assertEqual(result["path"], "cam/Gate-North")
        self.assertTrue(result["pushed_to_mediamtx"])
        self.assertEqual(self.mtx.paths["cam/Gate-North"], "rtsp://10.0.0.5/cam")
        streams = self.reg.list_streams()
        self.assertEqual(streams[0]["ready"], True)

    def test_camera_with_position_becomes_map_marker(self):
        self.reg.register_camera("gate", "rtsp://x/y", lat=38.9, lon=-77.0)
        dev = self.store.device("CAM.gate")
        self.assertIsNotNone(dev)
        self.assertEqual(dev["role"], "Camera")
        rows = self.reg.streams_for_device("CAM.gate")
        self.assertEqual(len(rows), 1)

    def test_cot_video_dedup(self):
        self.reg.register_cot_video("U1", "rtsp://a/1")
        self.reg.register_cot_video("U1", "rtsp://a/1")  # dedup: no re-push
        rows = self.reg.streams_for_device("U1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "cot/U1")

    def test_remove(self):
        self.reg.register_camera("g", "rtsp://x/y")
        self.reg.remove("cam/g")
        self.assertEqual(self.reg.list_streams(), [])
        self.assertNotIn("cam/g", self.mtx.paths)

    def test_unknown_action_denied(self):
        self.assertFalse(self.reg.authorize({"action": "weird"}))

    def test_record_toggle_reflects_in_stream_rows(self):
        # a device publishes -> stream registered, not recording by default
        self.reg.authorize({"action": "publish", "path": "live/DEV-1",
                            "ip": "10.0.0.9"})
        rows = self.reg.streams_for_device("DEV-1")
        self.assertFalse(rows[0]["recording"])
        # turn recording on -> reflected in the rows and in list_streams
        self.assertTrue(self.reg.set_record("live/DEV-1", True))
        self.assertTrue(
            self.reg.streams_for_device("DEV-1")[0]["recording"])
        self.assertTrue(self.reg.list_streams()[0]["recording"])
        # turn it back off
        self.reg.set_record("live/DEV-1", False)
        self.assertFalse(
            self.reg.streams_for_device("DEV-1")[0]["recording"])


if __name__ == "__main__":
    unittest.main()
