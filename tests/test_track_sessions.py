"""Recording a device's track.

A session is a bookmark over the position stream, not a copy of it: positions
are already stored for every device, so a recording only says which devices and
which window. That makes starting one instant and lets a recording survive a
restart - but it also means retention has to leave a saved session's points
alone.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.store import Store  # noqa: E402


def _state(uid, lat=38.9, lon=-77.0):
    return dict(uid=uid, callsign=uid, cot_type="a-f-G-U-C", team="Cyan",
                role="Team Member", device="ATAK", platform="Android",
                lat=lat, lon=lon, hae=0.0, course=0.0, speed=0.0,
                battery=90, video_url=None)


class TestSessionLifecycle(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_starting_a_session_returns_it_open(self):
        s = self.store.start_track_session(["DEV-A"], name="north patrol")
        self.assertIsInstance(s["id"], int)
        self.assertEqual(s["name"], "north patrol")
        self.assertEqual(s["devices"], ["DEV-A"])
        self.assertIsNone(s["ended"])
        self.assertGreater(s["started"], 0)

    def test_an_open_session_is_listed_as_recording(self):
        self.store.start_track_session(["DEV-A"])
        rows = self.store.track_sessions()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["recording"])

    def test_stopping_sets_the_end_and_clears_recording(self):
        s = self.store.start_track_session(["DEV-A"])
        self.assertTrue(self.store.stop_track_session(s["id"]))
        row = self.store.track_sessions()[0]
        self.assertIsNotNone(row["ended"])
        self.assertFalse(row["recording"])

    def test_stopping_an_unknown_session_reports_false(self):
        self.assertFalse(self.store.stop_track_session(4242))

    def test_stopping_twice_does_not_move_the_end(self):
        s = self.store.start_track_session(["DEV-A"])
        self.store.stop_track_session(s["id"])
        first_end = self.store.track_sessions()[0]["ended"]
        self.store.stop_track_session(s["id"])
        self.assertEqual(self.store.track_sessions()[0]["ended"], first_end)

    def test_empty_device_list_means_every_device(self):
        """'Select all' has to include devices that appear mid-recording, so it
        is stored as [] rather than a snapshot of the current roster."""
        s = self.store.start_track_session([])
        self.assertEqual(s["devices"], [])
        self.assertTrue(self.store.track_sessions()[0]["all_devices"])

    def test_sessions_are_listed_newest_first(self):
        a = self.store.start_track_session(["DEV-A"], name="first")
        time.sleep(0.01)
        b = self.store.start_track_session(["DEV-B"], name="second")
        self.assertEqual([r["id"] for r in self.store.track_sessions()],
                         [b["id"], a["id"]])

    def test_duration_of_a_closed_session(self):
        s = self.store.start_track_session(["DEV-A"])
        self.store._db.execute(
            "UPDATE track_sessions SET started = ?, ended = ? WHERE id = ?",
            (1000.0, 1090.0, s["id"]))
        self.store._db.commit()
        self.assertAlmostEqual(self.store.track_sessions()[0]["duration"], 90.0)

    def test_a_recording_session_reports_elapsed_time_so_far(self):
        s = self.store.start_track_session(["DEV-A"])
        self.store._db.execute("UPDATE track_sessions SET started = ? WHERE id = ?",
                               (time.time() - 30, s["id"]))
        self.store._db.commit()
        self.assertGreaterEqual(self.store.track_sessions()[0]["duration"], 29)

    def test_deleting_removes_the_bookmark_but_keeps_the_positions(self):
        self.store.upsert_device(_state("DEV-A"))
        s = self.store.start_track_session(["DEV-A"])
        self.store.stop_track_session(s["id"])
        self.assertTrue(self.store.delete_track_session(s["id"]))
        self.assertEqual(self.store.track_sessions(), [])
        self.assertTrue(self.store.track("DEV-A"))


class TestSessionPointCounts(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_counts_only_points_inside_the_window_and_device_set(self):
        now = time.time()
        for uid in ("DEV-A", "DEV-B"):
            self.store.upsert_device(_state(uid))
        s = self.store.start_track_session(["DEV-A"])
        self.store._db.execute(
            "UPDATE track_sessions SET started = ?, ended = ? WHERE id = ?",
            (now - 100, now - 50, s["id"]))
        # inside the window, right device
        self.store._db.execute("INSERT INTO positions (uid, ts, lat, lon, hae)"
                               " VALUES ('DEV-A', ?, 1, 1, 0)", (now - 75,))
        # inside the window, wrong device
        self.store._db.execute("INSERT INTO positions (uid, ts, lat, lon, hae)"
                               " VALUES ('DEV-B', ?, 1, 1, 0)", (now - 75,))
        # right device, outside the window
        self.store._db.execute("INSERT INTO positions (uid, ts, lat, lon, hae)"
                               " VALUES ('DEV-A', ?, 1, 1, 0)", (now - 10,))
        self.store._db.commit()
        self.assertEqual(self.store.track_sessions()[0]["points"], 1)


class TestHistoryWindow(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.now = time.time()
        # positions only: upsert_device would add a row of its own at "now"
        for uid, offset in (("DEV-A", -300), ("DEV-A", -100), ("DEV-B", -100)):
            self.store._db.execute(
                "INSERT INTO positions (uid, ts, lat, lon, hae) VALUES (?,?,1,1,0)",
                (uid, self.now + offset))
        self.store._db.commit()

    def test_window_filters_by_time(self):
        rows = self.store.history(since=self.now - 200, until=self.now)
        self.assertEqual(len(rows), 2)

    def test_window_filters_by_device(self):
        rows = self.store.history(since=self.now - 400, until=self.now,
                                  uids=["DEV-A"])
        self.assertEqual({r["uid"] for r in rows}, {"DEV-A"})
        self.assertEqual(len(rows), 2)

    def test_minutes_still_works(self):
        self.assertEqual(len(self.store.history(minutes=10)), 3)


class TestRetentionProtectsSavedTracks(unittest.TestCase):
    def setUp(self):
        # no upsert_device here: it writes a fresh position row of its own,
        # which would muddy "everything old was pruned"
        self.store = Store(":memory:")
        self.old = time.time() - 30 * 86400

    def _old_point(self, ts):
        self.store._db.execute(
            "INSERT INTO positions (uid, ts, lat, lon, hae) VALUES ('DEV-A',?,1,1,0)",
            (ts,))
        self.store._db.commit()

    def test_old_points_are_pruned_normally(self):
        self._old_point(self.old)
        self.assertEqual(self.store.prune_positions(7), 1)
        self.assertEqual(len(self.store.track("DEV-A")), 0)

    def test_points_inside_a_saved_session_survive_the_prune(self):
        """A kept recording is a statement that those points matter; retention
        must not silently empty it."""
        self._old_point(self.old)
        s = self.store.start_track_session(["DEV-A"])
        self.store._db.execute(
            "UPDATE track_sessions SET started = ?, ended = ? WHERE id = ?",
            (self.old - 60, self.old + 60, s["id"]))
        self.store._db.commit()
        self.assertEqual(self.store.prune_positions(7), 0)
        self.assertEqual(len(self.store.track("DEV-A")), 1)

    def test_an_all_devices_session_protects_every_device(self):
        self._old_point(self.old)
        s = self.store.start_track_session([])
        self.store._db.execute(
            "UPDATE track_sessions SET started = ?, ended = ? WHERE id = ?",
            (self.old - 60, self.old + 60, s["id"]))
        self.store._db.commit()
        self.assertEqual(self.store.prune_positions(7), 0)

    def test_points_outside_the_session_window_are_still_pruned(self):
        self._old_point(self.old)
        self._old_point(self.old + 10000)
        s = self.store.start_track_session(["DEV-A"])
        self.store._db.execute(
            "UPDATE track_sessions SET started = ?, ended = ? WHERE id = ?",
            (self.old - 60, self.old + 60, s["id"]))
        self.store._db.commit()
        self.assertEqual(self.store.prune_positions(7), 1)
        self.assertEqual(len(self.store.track("DEV-A")), 1)


if __name__ == "__main__":
    unittest.main()
