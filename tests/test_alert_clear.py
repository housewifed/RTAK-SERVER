"""Clearing an emergency from the web UI.

The banner's X calls DELETE /api/alerts, which clears the alert server-side
and broadcasts a cancel CoT so the phones stop alerting too. ATAK's 911 event
carries its own uid (<device>-9-1-1), which is why an alert is keyed on that
uid and not on the device's - deleting the device never cleared it.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from takcore.cot import build_emergency_cancel, parse_event  # noqa: E402
from takcore.hub import Hub  # noqa: E402
from takcore.store import Store  # noqa: E402

RAISE = (
    b'<event version="2.0" uid="ANDROID-1-9-1-1" type="b-a-o-tbl" how="m-g" '
    b'time="2026-07-16T12:00:00.000Z" start="2026-07-16T12:00:00.000Z" '
    b'stale="2026-07-16T12:10:00.000Z">'
    b'<point lat="38.91" lon="-77.02" hae="10" ce="5" le="9999999"/>'
    b'<detail><emergency type="911 Alert">VIPER-1</emergency>'
    b'<contact callsign="VIPER-1"/></detail></event>'
)


class TestEmergencyCancelBuilder(unittest.TestCase):
    def test_round_trips_through_the_parser(self):
        ev = parse_event(build_emergency_cancel("ANDROID-1-9-1-1", "VIPER-1"))
        self.assertEqual(ev.uid, "ANDROID-1-9-1-1")
        self.assertEqual(ev.type, "b-a-o-can")
        self.assertTrue(ev.is_emergency)
        self.assertTrue(ev.emergency_cancel)

    def test_carries_the_callsign(self):
        ev = parse_event(build_emergency_cancel("X-9-1-1", "VIPER-1"))
        self.assertEqual(ev.callsign, "VIPER-1")

    def test_falls_back_to_the_uid_when_no_callsign(self):
        ev = parse_event(build_emergency_cancel("X-9-1-1"))
        self.assertEqual(ev.callsign, "X-9-1-1")

    def test_escapes_xml_in_the_callsign(self):
        raw = build_emergency_cancel("X-9-1-1", 'A&B<"C>')
        ev = parse_event(raw)          # must still parse
        self.assertEqual(ev.callsign, 'A&B<"C>')


class TestClearingAnAlert(unittest.TestCase):
    def setUp(self):
        self.hub = Hub(Store(":memory:"))
        self.store = self.hub.store

    def _raise(self):
        self.hub.publish(parse_event(RAISE), origin=None)

    def test_alert_is_keyed_on_the_emergency_uid(self):
        self._raise()
        active = self.store.active_alerts()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["uid"], "ANDROID-1-9-1-1")
        self.assertEqual(active[0]["callsign"], "VIPER-1")

    def test_clear_alert_empties_the_active_list(self):
        self._raise()
        self.store.clear_alert("ANDROID-1-9-1-1")
        self.assertEqual(self.store.active_alerts(), [])

    def test_clearing_an_unknown_uid_is_harmless(self):
        self._raise()
        self.store.clear_alert("not-a-real-uid")
        self.assertEqual(len(self.store.active_alerts()), 1)

    def test_the_cancel_we_broadcast_clears_our_own_alert(self):
        """The cancel CoT sent to devices must be one this server would honour
        if a phone sent it back - otherwise web and device state can diverge."""
        self._raise()
        cancel = build_emergency_cancel("ANDROID-1-9-1-1", "VIPER-1")
        self.hub.publish(parse_event(cancel), origin=None)
        self.assertEqual(self.store.active_alerts(), [])

    def test_deleting_the_device_does_not_clear_the_911(self):
        """Regression: the device uid and the alert uid differ, so removing the
        device leaves the alert on the map. The web UI needs its own control."""
        self._raise()
        self.store.delete_device("ANDROID-1")
        self.assertEqual(len(self.store.active_alerts()), 1)


if __name__ == "__main__":
    unittest.main()
