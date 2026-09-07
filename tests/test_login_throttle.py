import unittest

from takcore.auth import LoginThrottle


class Clock:
    def __init__(self):
        self.t = 1000.0
    def __call__(self):
        return self.t


class TestLoginThrottle(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.th = LoginThrottle(max_fails=3, window=900.0, lockout=60.0,
                                now_fn=self.clock)
        self.key = ("1.2.3.4", "admin")

    def test_not_locked_initially(self):
        self.assertIsNone(self.th.check(self.key))

    def test_locks_after_max_fails(self):
        for _ in range(3):
            self.th.record_failure(self.key)
        rem = self.th.check(self.key)
        self.assertIsNotNone(rem)
        self.assertTrue(0 < rem <= 60.0)

    def test_success_clears(self):
        for _ in range(3):
            self.th.record_failure(self.key)
        self.th.record_success(self.key)
        self.assertIsNone(self.th.check(self.key))

    def test_lock_expires_after_lockout(self):
        for _ in range(3):
            self.th.record_failure(self.key)
        self.clock.t += 61.0
        self.assertIsNone(self.th.check(self.key))

    def test_old_failures_fall_out_of_window(self):
        self.th.record_failure(self.key)
        self.th.record_failure(self.key)
        self.clock.t += 901.0  # first two age out
        self.th.record_failure(self.key)  # only 1 fresh failure
        self.assertIsNone(self.th.check(self.key))

    def test_stale_keys_evicted(self):
        other = ("9.9.9.9", "ghost")
        self.th.record_failure(other)
        self.clock.t += 901.0           # 'other' ages out of the window
        self.th.record_failure(self.key)  # triggers eviction sweep
        self.assertNotIn(other, self.th._fails)


if __name__ == "__main__":
    unittest.main()
