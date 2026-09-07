import time
import unittest

from takcore.store import Store


class TestPositionRetention(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def _insert_position(self, uid, ts):
        with self.store._lock:
            self.store._db.execute(
                "INSERT INTO positions (uid, ts, lat, lon, hae) "
                "VALUES (?,?,?,?,?)", (uid, ts, 1.0, 2.0, None))
            self.store._db.commit()

    def test_prune_removes_only_old_rows(self):
        now = time.time()
        self._insert_position("FRESH", now - 1 * 3600)       # 1 h old
        self._insert_position("OLD", now - 10 * 86400)       # 10 d old
        removed = self.store.prune_positions(7)              # keep 7 days
        self.assertEqual(removed, 1)
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT uid FROM positions").fetchall()
        self.assertEqual([r[0] for r in rows], ["FRESH"])

    def test_prune_noop_when_all_fresh(self):
        self._insert_position("A", time.time())
        self.assertEqual(self.store.prune_positions(7), 0)


if __name__ == "__main__":
    unittest.main()
