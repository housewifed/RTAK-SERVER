"""Unit tests for the CoT parser. Run:  python3 -m unittest discover server/tests"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.cot import CotStreamParser, build_pong, parse_event  # noqa: E402

# A realistic ATAK self-SA event
ATAK_SA = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<event version="2.0" uid="ANDROID-abc123" type="a-f-G-U-C" how="m-g" '
    b'time="2026-07-05T12:00:00.000Z" start="2026-07-05T12:00:00.000Z" '
    b'stale="2026-07-05T12:06:15.000Z">'
    b'<point lat="38.8977" lon="-77.0365" hae="18.0" ce="4.9" le="9999999.0"/>'
    b'<detail>'
    b'<takv os="34" version="5.2.0" device="PIXEL 8" platform="ATAK-CIV"/>'
    b'<contact callsign="VIPER-1" endpoint="*:-1:stcp"/>'
    b'<uid Droid="VIPER-1"/>'
    b'<__group role="Team Lead" name="Cyan"/>'
    b'<status battery="87"/>'
    b'<track course="123.4" speed="1.5"/>'
    b'</detail></event>'
)

PING = (
    b'<event version="2.0" uid="ping-1" type="t-x-c-t" how="m-g" '
    b'time="2026-07-05T12:00:00.000Z" start="2026-07-05T12:00:00.000Z" '
    b'stale="2026-07-05T12:00:10.000Z">'
    b'<point lat="0.0" lon="0.0" hae="0.0" ce="9999999" le="9999999"/></event>'
)

VIDEO_SA = (
    b'<event version="2.0" uid="CAM-7" type="a-f-G-E-S" how="m-g" '
    b'time="2026-07-05T12:00:00.000Z" start="2026-07-05T12:00:00.000Z" '
    b'stale="2026-07-05T12:06:00.000Z">'
    b'<point lat="38.9" lon="-77.03" hae="12" ce="5" le="5"/>'
    b'<detail><__video url="rtsp://10.0.0.5:8554/live/cam7"/></detail></event>'
)


class TestParseEvent(unittest.TestCase):
    def test_atak_sa(self):
        ev = parse_event(ATAK_SA)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.uid, "ANDROID-abc123")
        self.assertEqual(ev.type, "a-f-G-U-C")
        self.assertEqual(ev.callsign, "VIPER-1")
        self.assertEqual(ev.group_name, "Cyan")
        self.assertEqual(ev.group_role, "Team Lead")
        self.assertAlmostEqual(ev.lat, 38.8977)
        self.assertAlmostEqual(ev.lon, -77.0365)
        self.assertIsNone(ev.le)  # 9999999 sentinel -> None
        self.assertEqual(ev.battery, 87)
        self.assertEqual(ev.device, "PIXEL 8")
        self.assertAlmostEqual(ev.speed, 1.5)
        self.assertTrue(ev.is_position)
        self.assertFalse(ev.is_ping)

    def test_ping(self):
        ev = parse_event(PING)
        self.assertTrue(ev.is_ping)
        self.assertFalse(ev.is_position)  # 0,0 is not a real position

    def test_video_extension(self):
        ev = parse_event(VIDEO_SA)
        self.assertEqual(ev.video_url, "rtsp://10.0.0.5:8554/live/cam7")

    def test_garbage(self):
        self.assertIsNone(parse_event(b"<event this is not xml"))
        self.assertIsNone(parse_event(b"<foo/>"))

    def test_pong_is_valid_cot(self):
        ev = parse_event(build_pong())
        self.assertIsNotNone(ev)
        self.assertEqual(ev.type, "t-x-c-t-r")


class TestStreamParser(unittest.TestCase):
    def test_single_event(self):
        p = CotStreamParser()
        out = p.feed(ATAK_SA)
        self.assertEqual(len(out), 1)
        self.assertIsNotNone(parse_event(out[0]))

    def test_concatenated_events(self):
        p = CotStreamParser()
        out = p.feed(ATAK_SA + PING + VIDEO_SA)
        self.assertEqual(len(out), 3)

    def test_chunked_delivery(self):
        """Events split at arbitrary byte boundaries must reassemble."""
        stream = ATAK_SA + PING + VIDEO_SA
        for chunk_size in (1, 3, 7, 17, 64, 200):
            p = CotStreamParser()
            out = []
            for i in range(0, len(stream), chunk_size):
                out.extend(p.feed(stream[i : i + chunk_size]))
            self.assertEqual(len(out), 3, f"chunk_size={chunk_size}")
            self.assertEqual(parse_event(out[0]).uid, "ANDROID-abc123")
            self.assertEqual(parse_event(out[2]).uid, "CAM-7")

    def test_self_closing_event(self):
        p = CotStreamParser()
        out = p.feed(b'<event uid="x" type="t-x-c-t" version="2.0"/>')
        self.assertEqual(len(out), 1)

    def test_xml_declaration_skipped(self):
        p = CotStreamParser()
        out = p.feed(b'<?xml version="1.0"?>' + PING)
        self.assertEqual(len(out), 1)

    def test_auth_blob_ignored(self):
        p = CotStreamParser()
        out = p.feed(b'<auth><cot username="u" password="p"/></auth>' + PING)
        self.assertEqual(len(out), 1)
        self.assertTrue(parse_event(out[0]).is_ping)


if __name__ == "__main__":
    unittest.main()
