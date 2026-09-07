"""Store-level tests for admin device management (remove one / remove all).

Backs the "Devices" admin panel: deleting a device must also clear the data
tied to it (track history, active alerts, device-attached video streams) so a
removed device leaves nothing stale behind on the map.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.store import Store  # noqa: E402


def _device_state(uid, **over):
    """A full device state dict (upsert_device uses named params, so every
    column must be present)."""
    base = dict(uid=uid, callsign=uid, cot_type="a-f-G-U-C", team="Cyan",
                role="Team Member", device="ATAK", platform="Android",
                lat=38.9, lon=-77.0, hae=0.0, course=0.0, speed=0.0,
                battery=90, video_url=None)
    base.update(over)
    return base


class TestDeviceManagement(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_delete_device_removes_device_and_its_history(self):
        self.store.upsert_device(_device_state("DEV-1"))
        self.store.upsert_device(_device_state("DEV-2"))
        # DEV-1 also has an active alert and a device-attached video stream
        self.store.raise_alert("DEV-1", "DEV-1", "911", 38.9, -77.0)
        self.store.upsert_stream({"path": "live/DEV-1", "device_uid": "DEV-1",
                                  "kind": "device", "source": None,
                                  "name": None})

        existed = self.store.delete_device("DEV-1")

        self.assertTrue(existed)
        self.assertEqual([d["uid"] for d in self.store.devices()], ["DEV-2"])
        self.assertEqual(self.store.track("DEV-1"), [])          # breadcrumbs
        self.assertEqual(self.store.active_alerts(), [])         # alert cleared
        self.assertEqual(self.store.streams_for_device("DEV-1"), [])
        # untouched neighbour keeps its position history
        self.assertTrue(len(self.store.track("DEV-2")) >= 1)

    def test_delete_device_unknown_returns_false(self):
        self.assertFalse(self.store.delete_device("NOPE"))

    def test_delete_all_devices_clears_everything_and_returns_count(self):
        self.store.upsert_device(_device_state("A"))
        self.store.upsert_device(_device_state("B"))
        self.store.raise_alert("A", "A", "911", 1.0, 2.0)

        removed = self.store.delete_all_devices()

        self.assertEqual(removed, 2)
        self.assertEqual(self.store.devices(), [])
        self.assertEqual(self.store.history(1440), [])          # all positions
        self.assertEqual(self.store.active_alerts(), [])

    def test_delete_all_devices_on_empty_returns_zero(self):
        self.assertEqual(self.store.delete_all_devices(), 0)

    def test_device_count(self):
        self.assertEqual(self.store.device_count(), 0)
        self.store.upsert_device(_device_state("A"))
        self.store.upsert_device(_device_state("B"))
        self.assertEqual(self.store.device_count(), 2)


if __name__ == "__main__":
    unittest.main()
