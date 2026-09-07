import unittest

from tests.simulate_atak import _extract_event_times


class TestExtractEventTimes(unittest.TestCase):
    def test_parses_time_attribute(self):
        chunk = (b'<event version="2.0" uid="X" type="a-f-G-U-C" '
                 b'time="2026-07-19T12:00:00.000Z" '
                 b'start="2026-07-19T12:00:00.000Z" '
                 b'stale="2026-07-19T12:01:15.000Z"><point/></event>')
        times = _extract_event_times(chunk)
        self.assertEqual(len(times), 1)
        # 2026-07-19T12:00:00Z epoch
        self.assertAlmostEqual(times[0], 1784462400.0, delta=1.0)

    def test_ignores_chunks_without_time(self):
        self.assertEqual(_extract_event_times(b"<takPing/>"), [])
