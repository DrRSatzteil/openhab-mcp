import os
import unittest
from datetime import datetime
from unittest import mock

from openhab_mcp.logs import _parse_time


class TestRelativeTimeUsesConfiguredLogTimezone(unittest.TestCase):
    """Regression test: relative windows ('1h', '30m') must be computed in
    openHAB's own log timezone (OPENHAB_LOG_TZ), not this process's ambient
    OS timezone — otherwise, if the two differ (e.g. this container defaults
    to UTC while openHAB logs in Europe/Berlin), every relative/absolute
    window silently shifts by the zone offset with no error."""

    def test_relative_window_reflects_configured_tz_not_utc(self):
        with mock.patch.dict(os.environ, {"OPENHAB_LOG_TZ": "Europe/Berlin"}):
            berlin_now = _parse_time("0h")

        with mock.patch.dict(os.environ, {"OPENHAB_LOG_TZ": "UTC"}):
            utc_now = _parse_time("0h")

        # Europe/Berlin is UTC+1 or UTC+2 (DST) — never equal to UTC, and the
        # naive wall-clock difference must land on a whole hour.
        delta_hours = (berlin_now - utc_now).total_seconds() / 3600
        self.assertIn(round(delta_hours, 2), (1.0, 2.0))

    def test_absolute_iso_value_is_taken_as_given(self):
        parsed = _parse_time("2026-09-16T18:19:00")
        self.assertEqual(parsed, datetime(2026, 9, 16, 18, 19, 0))


if __name__ == "__main__":
    unittest.main()
