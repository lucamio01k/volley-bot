"""Inspection, preview and manual-send commands."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from zoneinfo import ZoneInfo

import config
import database
from api_sports_client import APISportsClient
from tasks import (
    sync_schedule,
    task_send_daily,
    task_send_reminders,
    task_send_results,
    task_send_weekly,
)
from telegram_sender import send_message, test_connection


KNOWN_2026_FRIENDLIES = {
    "international-friendlies-2026-men": {
        ("2026-07-05", "ARGENTINA"),
        ("2026-08-26", "GERMANY"),
    },
    "international-friendlies-2026-women": {
        ("2026-05-14", "FRANCE"),
        ("2026-05-15", "FRANCE"),
    },
}


def _date(value: str | None) -> dt.date | None:
    return dt.date.fromisoformat(value) if value else None


def _print_matches(tracked_only: bool) -> None:
    rows = database.list_matches(
        config.DB_PATH,
        config.TRACKED_TEAM if tracked_only else None,
    )
    if not rows:
        print("No cached matches.")
        return
    local_tz = ZoneInfo(config.BOT_TIMEZONE)
    for match in rows:
        when = "TBD"
        if match.scheduled_at_utc:
            when = (
                dt.datetime.fromisoformat(match.scheduled_at_utc)
                .astimezone(local_tz)
                .strftime("%Y-%m-%d %H:%M")
            )
        print(
            f"{match.stable_key:<42} | {when} | {match.status:<9} | "
            f"{match.home_team} {match.home_sets}-{match.away_sets} {match.away_team} | {match.phase}"
        )


def _missing_known_friendlies(competition_key: str, rows) -> set[tuple[str, str]]:
    expected = KNOWN_2026_FRIENDLIES.get(competition_key, set())
    found: set[tuple[str, str]] = set()
    for row in rows:
        if not row.scheduled_at_utc:
            continue
        date = dt.datetime.fromisoformat(
            row.scheduled_at_utc.replace("Z", "+00:00")
        ).date().isoformat()
        opponent = row.opponent_of(config.TRACKED_TEAM).upper()
        for expected_date, expected_opponent in expected:
            if date == expected_date and opponent.startswith(expected_opponent):
                found.add((expected_date, expected_opponent))
    return expected - found


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Italia Volley Telegram bot CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    sync = commands.add_parser("sync", help="Refresh the CEV cache")
    sync.add_argument("--send", action="store_true", help="Send detected fixture/change notifications")
    sync.add_argument("--force", action="store_true", help="Ignore refresh interval")

    commands.add_parser("matches", help="List cached Italy matches")
    commands.add_parser("bracket", help="List the complete cached bracket")

    for name, help_text in (("week", "Preview/send weekly digest"), ("today", "Preview/send daily digest")):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--date", help="Reference date YYYY-MM-DD")
        sub.add_argument("--send", action="store_true")
        sub.add_argument("--force", action="store_true")

    reminders = commands.add_parser("reminders", help="Preview/send reminders currently due")
    reminders.add_argument("--send", action="store_true")
    reminders.add_argument("--force", action="store_true")

    results = commands.add_parser("results", help="Fetch and preview/send due results")
    results.add_argument("--send", action="store_true")
    results.add_argument("--force", action="store_true")

    source_check = commands.add_parser(
        "source-check", help="Validate live providers without changing cache"
    )
    source_check.add_argument(
        "--all", action="store_true", help="Also validate disabled provider configurations"
    )
    commands.add_parser("api-sports-probe", help="Discover API-Sports friendly league/team IDs")
    commands.add_parser("alerts-status", help="Show operational incident state")
    test = commands.add_parser("test", help="Test Telegram connection")
    test.add_argument("--send", action="store_true", help="Send a visible test message")
    commands.add_parser("db-status", help="Show database counters")
    return parser


def main() -> None:
    database.init_db(config.DB_PATH)
    args = build_parser().parse_args()

    if args.command == "sync":
        totals = sync_schedule(force=args.force, notify=args.send)
        print("\n".join(f"{key}: {count} matches" for key, count in totals.items()) or "No sync due/succeeded.")
    elif args.command == "matches":
        _print_matches(True)
    elif args.command == "bracket":
        _print_matches(False)
    elif args.command == "week":
        print(task_send_weekly(reference_date=_date(args.date), force=args.force, send=args.send))
    elif args.command == "today":
        print(task_send_daily(reference_date=_date(args.date), force=args.force, send=args.send))
    elif args.command == "reminders":
        messages = task_send_reminders(force=args.force, send=args.send)
        print("\n\n".join(messages) if messages else "No reminders due.")
    elif args.command == "results":
        messages = task_send_results(force=args.force, send=args.send)
        print("\n\n".join(messages) if messages else "No final results available/due.")
    elif args.command == "source-check":
        failed = False
        for competition in config.COMPETITIONS:
            if not competition.enabled and not args.all:
                print(f"SKIP {competition.key}: disabled")
                continue
            try:
                from providers import create_provider

                client = create_provider(
                    competition,
                    int(config.section("source_refresh").get("request_timeout_seconds", 20)),
                )
                rows = client.fetch_schedule(competition).matches
                italy = sum(row.involves(config.TRACKED_TEAM) for row in rows)
                missing = _missing_known_friendlies(competition.key, rows)
                if missing:
                    cases = ", ".join(
                        f"{date} {opponent}" for date, opponent in sorted(missing)
                    )
                    raise RuntimeError(f"known friendlies missing: {cases}")
                print(f"OK {competition.key}: {len(rows)} matches, {italy} involving Italy")
            except Exception as exc:
                failed = True
                print(f"FAIL {competition.key}: {exc.__class__.__name__}: {exc}")
        if failed:
            raise SystemExit(1)
    elif args.command == "api-sports-probe":
        client = APISportsClient(
            config.API_SPORTS_VOLLEYBALL_KEY,
            int(config.section("source_refresh").get("request_timeout_seconds", 20)),
        )
        print(json.dumps(client.discover_identifiers(), ensure_ascii=False, indent=2))
    elif args.command == "alerts-status":
        states = database.list_component_states(config.DB_PATH)
        if not states:
            print("No component state recorded.")
        for state in states:
            print(
                f"{state['component']}: failures={state['failure_count']} "
                f"alert_sent={bool(state['alert_sent'])} last_success={state['last_success_at_utc']} "
                f"error={state['last_error']}"
            )
    elif args.command == "test":
        if not test_connection():
            raise SystemExit(1)
        print("Telegram connection OK.")
        if args.send and not send_message("<b>🏐 Italia Volley bot online</b>\nTest Telegram completato."):
            raise SystemExit(1)
    elif args.command == "db-status":
        print(f"Matches: {len(database.list_matches(config.DB_PATH))}")
        print(f"Notifications sent: {database.notification_count(config.DB_PATH)}")
        print(f"Components tracked: {len(database.list_component_states(config.DB_PATH))}")
        for state in database.list_provider_sync_states(config.DB_PATH):
            print(
                f"Provider {state['component']}: requests_today={state['requests_today']} "
                f"quota_remaining={state['quota_remaining']} last_success={state['last_success_at_utc']} "
                f"next_due={state['next_due_at_utc']}"
            )
    else:
        raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
