from __future__ import annotations

import datetime as dt
import html
import re
import unittest
from urllib.parse import parse_qs, urlparse

from formatter import format_match_line
from models import MatchRecord


def match(**overrides: object) -> MatchRecord:
    values: dict[str, object] = {
        "provider_match_id": "85100",
        "competition_key": "eurovolley-2026-women",
        "competition_id": 1573,
        "competition_name": "CEV EuroVolley 2026",
        "gender": "women",
        "match_code": "12972:002",
        "phase": "Quarter Final matches in CZE",
        "home_team": "ITALY",
        "away_team": "SWEDEN",
        "scheduled_at_utc": "2026-09-02T17:00:00+00:00",
        "local_timezone": "Europe/Prague",
        "venue": "",
    }
    values.update(overrides)
    return MatchRecord(**values)  # type: ignore[arg-type]


class CalendarLinkTests(unittest.TestCase):
    def test_calendar_link_last_ninety_minutes_from_match_start(self) -> None:
        text = format_match_line(match(), "Europe/Rome")
        href = re.search(r'<a href="([^"]+)">', text)

        self.assertIsNotNone(href)
        url = html.unescape(href.group(1))  # type: ignore[union-attr]
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
        self.assertEqual("calendar.google.com", parsed.netloc)
        self.assertEqual(["TEMPLATE"], query["action"])
        self.assertEqual(["20260902T170000Z/20260902T183000Z"], query["dates"])
        self.assertEqual("🏐 🇮🇹 Italy vs 🇸🇪 Sweden", query["text"][0])

    def test_unscheduled_or_cancelled_match_has_no_calendar_link(self) -> None:
        self.assertNotIn(
            "Aggiungi al calendario",
            format_match_line(match(scheduled_at_utc=None), "Europe/Rome"),
        )
        self.assertNotIn(
            "Aggiungi al calendario",
            format_match_line(match(status="cancelled"), "Europe/Rome"),
        )


if __name__ == "__main__":
    unittest.main()
