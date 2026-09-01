from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["LOG_FILE_PATH"] = os.devnull

import alerts
import database
import main
from models import MatchRecord
from tasks import task_send_results, task_send_weekly


class IncidentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "volley.db")
        database.init_db(self.db_path)
        self.messages: list[str] = []

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def sender(self, message: str) -> bool:
        self.messages.append(message)
        return True

    def test_three_failures_send_one_alert_and_recovery(self) -> None:
        for _ in range(2):
            alerts.record_failure(
                "cev:test", "HTTP timeout", db_path=self.db_path, sender=self.sender
            )
        self.assertEqual([], self.messages)

        alerts.record_failure(
            "cev:test", "HTTP timeout", db_path=self.db_path, sender=self.sender
        )
        alerts.record_failure(
            "cev:test", "HTTP timeout", db_path=self.db_path, sender=self.sender
        )
        self.assertEqual(1, len(self.messages))
        self.assertIn("Errore bot volley", self.messages[0])

        # The persisted state is the restart boundary: a later process sees the same incident.
        alerts.record_failure(
            "cev:test", "HTTP timeout", db_path=self.db_path, sender=self.sender
        )
        self.assertEqual(1, len(self.messages))

        alerts.record_success("cev:test", db_path=self.db_path, sender=self.sender)
        self.assertEqual(2, len(self.messages))
        self.assertIn("Bot volley ripristinato", self.messages[1])
        state = database.get_component_state(self.db_path, "cev:test")
        self.assertEqual(0, state["failure_count"])
        self.assertEqual(0, state["recovery_pending"])

    def test_isolated_failure_then_success_sends_nothing(self) -> None:
        alerts.record_failure(
            "cev:test", "one timeout", db_path=self.db_path, sender=self.sender
        )
        alerts.record_success("cev:test", db_path=self.db_path, sender=self.sender)

        self.assertEqual([], self.messages)
        state = database.get_component_state(self.db_path, "cev:test")
        self.assertEqual(0, state["failure_count"])

    def test_error_type_change_does_not_reset_component_outage(self) -> None:
        alerts.record_failure(
            "cev:test", "Timezone unknown", db_path=self.db_path, sender=self.sender
        )
        alerts.record_failure(
            "cev:test", "ConnectionError", db_path=self.db_path, sender=self.sender
        )
        alerts.record_failure(
            "cev:test", "CEV maintenance", db_path=self.db_path, sender=self.sender
        )

        self.assertEqual(1, len(self.messages))
        state = database.get_component_state(self.db_path, "cev:test")
        self.assertEqual(3, state["failure_count"])
        self.assertEqual("CEV maintenance", state["last_error"])

    def test_alert_redacts_tokens_tracebacks_and_html(self) -> None:
        unsafe = (
            "Traceback (most recent call last):\n"
            "  File '/private/bot.py', line 3\n"
            "RuntimeError: https://api.telegram.org/bot123456789:"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ123456/getMe <html><body>secret page</body></html>"
        )
        for _ in range(3):
            alerts.record_failure(
                "cev:test", unsafe, db_path=self.db_path, sender=self.sender
            )

        self.assertEqual(1, len(self.messages))
        message = self.messages[0]
        self.assertNotIn("123456789", message)
        self.assertNotIn("Traceback", message)
        self.assertNotIn("secret page", message)
        self.assertNotIn("&lt;html", message)
        self.assertIn("contenuto HTML omesso", message)

    def test_notification_is_persisted_only_after_telegram_success(self) -> None:
        task_send_weekly(
            send=True,
            db_path=self.db_path,
            sender=lambda _message: False,
        )
        self.assertEqual(0, database.notification_count(self.db_path))

        task_send_weekly(
            send=True,
            db_path=self.db_path,
            sender=lambda _message: True,
        )
        self.assertEqual(1, database.notification_count(self.db_path))

    def test_telegram_incident_is_reported_after_connection_recovers(self) -> None:
        for _ in range(3):
            alerts.record_failure(
                "telegram",
                "Connessione Telegram non disponibile",
                db_path=self.db_path,
                sender=lambda _message: False,
            )
        state = database.get_component_state(self.db_path, "telegram")
        self.assertEqual(1, state["alert_sent"])
        self.assertEqual([], self.messages)

        alerts.record_success("telegram", db_path=self.db_path, sender=self.sender)
        self.assertEqual(1, len(self.messages))
        self.assertIn("Bot volley ripristinato", self.messages[0])

    def test_failed_recovery_message_is_retried(self) -> None:
        for _ in range(3):
            alerts.record_failure(
                "cev:test", "HTTP timeout", db_path=self.db_path, sender=self.sender
            )
        self.messages.clear()

        alerts.record_success(
            "cev:test", db_path=self.db_path, sender=lambda _message: False
        )
        state = database.get_component_state(self.db_path, "cev:test")
        self.assertEqual(1, state["recovery_pending"])

        alerts.record_success("cev:test", db_path=self.db_path, sender=self.sender)
        self.assertEqual(1, len(self.messages))
        self.assertIn("Bot volley ripristinato", self.messages[0])
        state = database.get_component_state(self.db_path, "cev:test")
        self.assertEqual(0, state["recovery_pending"])

    def test_missing_result_after_five_hours_is_an_incident(self) -> None:
        now = dt.datetime.now(dt.timezone.utc)
        scheduled = now - dt.timedelta(hours=5, minutes=10)
        match = MatchRecord(
            provider_match_id="9999",
            competition_key="test-women",
            competition_id=1573,
            competition_name="CEV EuroVolley 2026",
            gender="women",
            match_code="1002:001",
            phase="Quarter Finals",
            home_team="ITALY",
            away_team="FRANCE",
            scheduled_at_utc=scheduled.replace(microsecond=0).isoformat(),
            local_timezone="Europe/Rome",
            venue="Arena di Assago",
            source_url="https://example.invalid/match",
        )
        database.upsert_matches(self.db_path, [match], "ITALY")

        for _ in range(3):
            task_send_results(
                now=now,
                send=False,
                db_path=self.db_path,
                sender=self.sender,
            )

        self.assertEqual(1, len(self.messages))
        self.assertIn("Risultato non disponibile entro 5 ore", self.messages[0])


class StartupTests(unittest.TestCase):
    @patch.object(main, "sync_schedule")
    @patch.object(main, "task_telegram_health")
    def test_startup_sync_keeps_change_notifications_enabled(
        self,
        health,
        sync,
    ) -> None:
        main._run_startup_tasks()

        health.assert_called_once_with()
        sync.assert_called_once_with(force=True, notify=True)


if __name__ == "__main__":
    unittest.main()
