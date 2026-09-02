from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LOG_FILE_PATH"] = os.devnull

import config
import database
from cev_client import CEVParseError, parse_competition_html, parse_match_detail_html
from models import CompetitionConfig
from tasks import _sync_due, sync_schedule


FIXTURES = Path(__file__).parent / "fixtures"
COMPETITION = CompetitionConfig(
    key="test-women",
    name="CEV EuroVolley 2026",
    gender="women",
    competition_id=1573,
    active_from="2026-08-21",
    active_to="2026-09-07",
)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class StaticClient:
    def __init__(self, value):
        self.value = value

    def fetch_competition(self, _competition):
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class ParserTests(unittest.TestCase):
    def test_parses_groups_tbd_and_winner_slots(self) -> None:
        matches = parse_competition_html(fixture("competition.html"), COMPETITION)

        self.assertEqual(10, len(matches))
        self.assertEqual("Pool A", matches[0].phase)
        self.assertEqual("ITALY", matches[0].home_team)
        self.assertEqual("2026-08-21T18:30:00+00:00", matches[0].scheduled_at_utc)
        self.assertEqual("Europe/Rome", matches[0].local_timezone)
        quarter = next(
            match
            for match in matches
            if match.source_payload["federation_match_code"] == "QF-01"
        )
        self.assertEqual("1002:001", quarter.match_code)
        self.assertEqual("Winner of Pool A", quarter.home_team)
        self.assertEqual("TBD", quarter.away_team)
        self.assertEqual("9006", quarter.provider_match_id)

    def test_parses_final_result_and_sets(self) -> None:
        match = parse_competition_html(fixture("competition.html"), COMPETITION)[0]
        detailed = parse_match_detail_html(fixture("match_final.html"), match)

        self.assertEqual("final", detailed.status)
        self.assertEqual((3, 1), (detailed.home_sets, detailed.away_sets))
        self.assertEqual([(25, 20), (22, 25), (25, 18), (25, 21)], detailed.set_scores)
        self.assertEqual("Assago", detailed.city)
        self.assertEqual("ITA", detailed.country_code)
        self.assertEqual("2026-09-03T18:30:00+00:00", detailed.scheduled_at_utc)

    def test_competition_score_is_marked_for_detail_fetch(self) -> None:
        scored = fixture("competition.html").replace(
            '<span id="m01_LB_SetCasa">-</span>',
            '<span id="m01_LB_SetCasa">3</span>',
        ).replace(
            '<span id="m01_LB_SetOspiti">-</span>',
            '<span id="m01_LB_SetOspiti">1</span>',
        )
        match = parse_competition_html(scored, COMPETITION)[0]
        self.assertEqual("result_available", match.status)

    def test_rejects_empty_or_unknown_structure(self) -> None:
        with self.assertRaises(CEVParseError):
            parse_competition_html("", COMPETITION)
        with self.assertRaises(CEVParseError):
            parse_competition_html(fixture("malformed.html"), COMPETITION)

    def test_keeps_partial_match_when_timezone_is_unknown(self) -> None:
        unknown = fixture("competition.html").replace("Arena di Modena", "Mystery Arena")
        matches = parse_competition_html(unknown, COMPETITION)

        self.assertIsNone(matches[0].scheduled_at_utc)
        self.assertIsNone(matches[0].local_timezone)
        self.assertEqual("21/08/2026 20:30", matches[0].source_payload["raw_local_datetime"])

    def test_phase_country_is_used_when_dated_match_has_no_venue(self) -> None:
        czech_quarter = fixture("competition.html").replace(
            'title="Quarter Finals"', 'title="Quarter Final matches in CZE"'
        ).replace(
            ">Quarter Finals<", ">Quarter Final matches in CZE<"
        ).replace(
            '<span id="m06_LB_Palasport">Arena di Assago</span>',
            '<span id="m06_LB_Palasport"></span>',
        )

        quarter = next(
            match
            for match in parse_competition_html(czech_quarter, COMPETITION)
            if match.source_payload["federation_match_code"] == "QF-01"
        )

        self.assertEqual("Europe/Prague", quarter.local_timezone)
        self.assertEqual("2026-09-03T18:30:00+00:00", quarter.scheduled_at_utc)

    def test_detail_reuses_saved_timezone_when_cev_omits_location(self) -> None:
        match = parse_competition_html(fixture("competition.html"), COMPETITION)[0]
        match.phase = "Quarter Finals"
        match.venue = ""
        detail = fixture("match_final.html")
        detail = (
            detail.replace("Arena di Assago", "")
            .replace("Assago", "")
            .replace("(ITA)", "()")
        )

        detailed = parse_match_detail_html(detail, match)

        self.assertEqual("Europe/Rome", detailed.local_timezone)
        self.assertEqual("2026-09-03T18:30:00+00:00", detailed.scheduled_at_utc)


class DatabaseTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "volley.db")
        database.init_db(self.db_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_tbd_assignment_and_changed_match_id_update_one_slot(self) -> None:
        initial_html = fixture("competition.html")
        assigned_html = initial_html.replace(
            ">Winner of Pool A<", ">ITALY<"
        ).replace("mID=9006", "mID=9906")

        database.upsert_matches(
            self.db_path,
            parse_competition_html(initial_html, COMPETITION),
            "ITALY",
        )
        changes = database.upsert_matches(
            self.db_path,
            parse_competition_html(assigned_html, COMPETITION),
            "ITALY",
        )

        quarter_change = next(
            change
            for change in changes
            if change.match.source_payload["federation_match_code"] == "QF-01"
        )
        self.assertTrue(quarter_change.became_tracked)
        self.assertFalse(quarter_change.is_new)
        self.assertEqual(10, len(database.list_matches(self.db_path)))
        stored = database.get_match(self.db_path, COMPETITION.key, "1002:001")
        self.assertIsNotNone(stored)
        self.assertEqual("ITALY", stored.home_team)
        self.assertEqual("9906", stored.provider_match_id)
        self.assertIsNotNone(stored.first_seen_at_utc)
        self.assertIsNotNone(stored.updated_at_utc)

    def test_sync_duration_does_not_skip_the_next_hourly_refresh(self) -> None:
        now = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.timezone.utc)
        state = {"last_success_at_utc": "2026-09-01T11:00:05+00:00"}

        with patch.object(database, "get_component_state", return_value=state):
            self.assertTrue(_sync_due(COMPETITION, now=now, db_path=self.db_path))

        state["last_success_at_utc"] = "2026-09-01T11:30:00+00:00"
        with patch.object(database, "get_component_state", return_value=state):
            self.assertFalse(_sync_due(COMPETITION, now=now, db_path=self.db_path))

    def test_date_opponent_or_venue_changes_are_detected(self) -> None:
        assigned = fixture("competition.html").replace(
            ">Winner of Pool A<", ">ITALY<"
        )
        database.upsert_matches(
            self.db_path,
            parse_competition_html(assigned, COMPETITION),
            "ITALY",
        )
        modified = assigned.replace(
            "03/09/2026 20:30", "03/09/2026 21:00", 1
        ).replace(">TBD<", ">FRANCE<", 1)
        changes = database.upsert_matches(
            self.db_path,
            parse_competition_html(modified, COMPETITION),
            "ITALY",
        )
        quarter_change = next(
            change
            for change in changes
            if change.match.source_payload["federation_match_code"] == "QF-01"
        )
        self.assertTrue(quarter_change.schedule_changed)

    def test_failed_sync_does_not_replace_valid_cache(self) -> None:
        rows = parse_competition_html(fixture("competition.html"), COMPETITION)
        with patch.object(config, "COMPETITIONS", [COMPETITION]):
            first = sync_schedule(
                force=True,
                notify=False,
                db_path=self.db_path,
                client=StaticClient(rows),
                sender=lambda _message: True,
            )
            second = sync_schedule(
                force=True,
                notify=False,
                db_path=self.db_path,
                client=StaticClient(CEVParseError("CEV response is empty")),
                sender=lambda _message: True,
            )

        self.assertEqual({COMPETITION.key: 10}, first)
        self.assertEqual({}, second)
        self.assertEqual(10, len(database.list_matches(self.db_path)))

    def test_incomplete_sync_does_not_partially_update_cache(self) -> None:
        rows = parse_competition_html(fixture("competition.html"), COMPETITION)
        changed_partial = parse_competition_html(
            fixture("competition.html").replace(">ITALY<", ">GERMANY<", 1),
            COMPETITION,
        )[:9]

        with patch.object(config, "COMPETITIONS", [COMPETITION]):
            sync_schedule(
                force=True,
                notify=False,
                db_path=self.db_path,
                client=StaticClient(rows),
                sender=lambda _message: True,
            )
            result = sync_schedule(
                force=True,
                notify=False,
                db_path=self.db_path,
                client=StaticClient(changed_partial),
                sender=lambda _message: True,
            )

        self.assertEqual({}, result)
        self.assertEqual(10, len(database.list_matches(self.db_path)))
        original = database.get_match(self.db_path, COMPETITION.key, "1001:001")
        self.assertEqual("ITALY", original.home_team)

    def test_stale_cache_becomes_a_deduplicated_incident(self) -> None:
        database.record_component_success(self.db_path, f"cev:{COMPETITION.key}")
        future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=13)
        messages: list[str] = []
        sender = lambda message: messages.append(message) or True

        with patch.object(config, "COMPETITIONS", [COMPETITION]):
            for _ in range(4):
                sync_schedule(
                    force=True,
                    notify=False,
                    now=future,
                    db_path=self.db_path,
                    client=StaticClient(CEVParseError("HTTP timeout")),
                    sender=sender,
                )

        cache_messages = [message for message in messages if "Cache troppo vecchia" in message]
        self.assertEqual(1, len(cache_messages))
        cache_state = database.get_component_state(
            self.db_path, f"cache:{COMPETITION.key}"
        )
        self.assertEqual(4, cache_state["failure_count"])

    def test_new_italy_slot_is_notified_once(self) -> None:
        initial = parse_competition_html(fixture("competition.html"), COMPETITION)
        assigned = parse_competition_html(
            fixture("competition.html").replace(">Winner of Pool A<", ">ITALY<"),
            COMPETITION,
        )
        messages: list[str] = []
        sender = lambda message: messages.append(message) or True
        now = dt.datetime(2026, 8, 29, 12, tzinfo=dt.timezone.utc)

        with patch.object(config, "COMPETITIONS", [COMPETITION]):
            sync_schedule(
                force=True,
                notify=False,
                now=now,
                db_path=self.db_path,
                client=StaticClient(initial),
                sender=sender,
            )
            sync_schedule(
                force=True,
                notify=True,
                now=now,
                db_path=self.db_path,
                client=StaticClient(assigned),
                sender=sender,
            )
            sync_schedule(
                force=True,
                notify=True,
                now=now,
                db_path=self.db_path,
                client=StaticClient(assigned),
                sender=sender,
            )

        fixture_messages = [message for message in messages if "Nuova partita confermata" in message]
        self.assertEqual(1, len(fixture_messages))


if __name__ == "__main__":
    unittest.main()
