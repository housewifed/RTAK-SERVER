"""Tests for GeoChat + emergency CoT parsing and the outgoing chat builder."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.cot import build_geochat, parse_event  # noqa: E402

GEOCHAT = (
    b'<event version="2.0" uid="GeoChat.ANDROID-1.All.abc" type="b-t-f" '
    b'how="h-g-i-g-o" time="2026-07-16T12:00:00.000Z" '
    b'start="2026-07-16T12:00:00.000Z" stale="2026-07-16T12:05:00.000Z">'
    b'<point lat="38.9" lon="-77.0" hae="0" ce="9999999" le="9999999"/>'
    b'<detail><__chat parent="RootContactGroup" chatroom="All Chat Rooms" '
    b'id="All Chat Rooms" senderCallsign="VIPER-1">'
    b'<chatgrp uid0="ANDROID-1" uid1="All Chat Rooms" id="All Chat Rooms"/>'
    b'</__chat><remarks source="BAO.F.ATAK.ANDROID-1">Contact left flank</remarks>'
    b'</detail></event>'
)

EMERGENCY = (
    b'<event version="2.0" uid="ANDROID-1-9-1-1" type="b-a-o-tbl" how="m-g" '
    b'time="2026-07-16T12:00:00.000Z" start="2026-07-16T12:00:00.000Z" '
    b'stale="2026-07-16T12:10:00.000Z">'
    b'<point lat="38.91" lon="-77.02" hae="10" ce="5" le="9999999"/>'
    b'<detail><emergency type="911 Alert">VIPER-1</emergency>'
    b'<contact callsign="VIPER-1"/></detail></event>'
)

EMERGENCY_CANCEL = (
    b'<event version="2.0" uid="ANDROID-1-9-1-1" type="b-a-o-can" how="m-g" '
    b'time="2026-07-16T12:00:00.000Z" start="2026-07-16T12:00:00.000Z" '
    b'stale="2026-07-16T12:10:00.000Z">'
    b'<point lat="38.91" lon="-77.02" hae="10" ce="5" le="9999999"/>'
    b'<detail><emergency cancel="true">VIPER-1</emergency></detail></event>'
)


class TestChat(unittest.TestCase):
    def test_parse_geochat(self):
        ev = parse_event(GEOCHAT)
        self.assertTrue(ev.is_chat)
        self.assertEqual(ev.chat_message, "Contact left flank")
        self.assertEqual(ev.chat_sender, "VIPER-1")
        self.assertEqual(ev.chat_sender_uid, "ANDROID-1")
        self.assertFalse(ev.is_emergency)

    def test_build_geochat_roundtrips(self):
        raw = build_geochat("Move to rally point", sender_callsign="BASE")
        ev = parse_event(raw)
        self.assertIsNotNone(ev)
        self.assertTrue(ev.is_chat)
        self.assertEqual(ev.chat_message, "Move to rally point")
        self.assertEqual(ev.chat_sender, "BASE")

    def test_build_geochat_escapes(self):
        raw = build_geochat('a<b>&"c"', sender_callsign="X")
        ev = parse_event(raw)  # must still parse (well-formed XML)
        self.assertEqual(ev.chat_message, 'a<b>&"c"')


class TestEmergency(unittest.TestCase):
    def test_parse_emergency(self):
        ev = parse_event(EMERGENCY)
        self.assertTrue(ev.is_emergency)
        self.assertFalse(ev.emergency_cancel)
        self.assertEqual(ev.emergency_type, "911 Alert")
        self.assertEqual(ev.callsign, "VIPER-1")
        self.assertAlmostEqual(ev.lat, 38.91)

    def test_parse_emergency_cancel(self):
        ev = parse_event(EMERGENCY_CANCEL)
        self.assertTrue(ev.is_emergency)
        self.assertTrue(ev.emergency_cancel)


if __name__ == "__main__":
    unittest.main()
