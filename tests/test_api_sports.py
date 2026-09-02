from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LOG_FILE_PATH"] = os.devnull

import config
import database
from api_sports_client import APISportsError, parse_games_payload
from models import CompetitionConfig, MatchRecord, ProviderBatch
from tasks import sync_schedule


FIXTURES = Path(__file__).parent / "fixtures"
COMPETITION = CompetitionConfig(
    key="international-friendlies-2026-men",
    name="Amichevole internazionale",
    gender="men",
    competition_id=99,
    provider="api_sports",
    provider_competition_id=99,
    tracked_team_id=6824,
    season=2026,
    enabled=True,
    active_interval_minutes=720,
    inactive_interval_minutes=720,
    active_from="2026-01-01",
    active_to="2026-12-31",
)


def payload() -> dict:
    return json.loads((FIXTURES / "api_sports_games.json").read_text(encoding="utf-8"))


class ParserTests(unittest.TestCase):
    def test_filters_exact_league_and_team_and_maps_final_score(self) -> None:
        matches, warnings = parse_games_payload(payload(), COMPETITION)

        self.assertEqual([], warnings)
        self.assertEqual(1, len(matches))
        match = matches[0]
        self.assertEqual("api_sports", match.provider)
        self.assertEqual("game:16933718", match.match_code)
        self.assertEqual(("ITALY", "GERMANY"), (match.home_team, match.away_team))
        self.assertEqual("final", match.status)
        self.assertEqual((3, 1), (match.home_sets, match.away_sets))
        self.assertEqual(4, len(match.set_scores))
        self.assertEqual("2026-08-26T19:00:00+00:00", match.scheduled_at_utc)

    def test_rejects_api_error_inside_http_200_payload(self) -> None:
        broken = payload()
        broken["errors"] = {"rateLimit": "Exceeded"}

        with self.assertRaises(APISportsError):
            parse_games_payload(broken, COMPETITION)


class ProviderIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "volley.db")
        database.init_db(self.db_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _match(self, status: str) -> MatchRecord:
        parsed, _ = parse_games_payload(payload(), COMPETITION)
        match = parsed[0]
        match.status = status
        if status != "final":
            match.home_sets = 0
            match.away_sets = 0
            match.set_scores = []
        return match

    def test_final_transition_sends_once_but_historical_final_does_not(self) -> None:
        class StaticProvider:
            def __init__(self, match):
                self.match = match

            def fetch_schedule(self, _competition):
                return ProviderBatch(matches=[self.match], quota_remaining=90)

        messages: list[str] = []
        sender = lambda message: messages.append(message) or True
        now = dt.datetime(2026, 8, 26, 18, tzinfo=dt.timezone.utc)
        with patch.object(config, "COMPETITIONS", [COMPETITION]):
            sync_schedule(
                force=True,
                notify=True,
                now=now,
                db_path=self.db_path,
                client=StaticProvider(self._match("scheduled")),
                sender=sender,
            )
            messages.clear()
            final_provider = StaticProvider(self._match("final"))
            sync_schedule(
                force=True,
                notify=True,
                now=now,
                db_path=self.db_path,
                client=final_provider,
                sender=sender,
            )
            sync_schedule(
                force=True,
                notify=True,
                now=now,
                db_path=self.db_path,
                client=final_provider,
                sender=sender,
            )

        result_messages = [message for message in messages if "risultato" in message]
        self.assertEqual(1, len(result_messages))
        self.assertIn("Amichevole internazionale", result_messages[0])

    def test_quota_reserve_prevents_provider_call(self) -> None:
        class CountingProvider:
            calls = 0

            def fetch_schedule(self, _competition):
                self.calls += 1
                return ProviderBatch(matches=[])

        database.record_provider_sync(
            self.db_path,
            "api_sports:international-friendlies-2026-men",
            success=True,
            quota_remaining=10,
        )
        provider = CountingProvider()
        with patch.object(config, "COMPETITIONS", [COMPETITION]):
            sync_schedule(
                force=True,
                notify=False,
                db_path=self.db_path,
                client=provider,
                sender=lambda _message: True,
            )

        self.assertEqual(0, provider.calls)

    def test_init_db_adds_provider_polling_columns(self) -> None:
        with database._connect(self.db_path) as conn:
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(matches)")
            }
        self.assertTrue(
            {"provider", "next_result_check_at_utc", "result_poll_attempts"}
            <= columns
        )


if __name__ == "__main__":
    unittest.main()
