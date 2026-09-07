"""SQLite persistence and idempotency helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from typing import Any, Iterable

from models import MatchChange, MatchRecord

NOTIFICATION_CLAIM_TIMEOUT = dt.timedelta(minutes=5)


def _connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider_match_id TEXT NOT NULL,
                competition_key TEXT NOT NULL,
                competition_id INTEGER NOT NULL,
                competition_name TEXT NOT NULL,
                gender TEXT NOT NULL,
                match_code TEXT NOT NULL,
                phase TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                scheduled_at_utc TEXT,
                local_timezone TEXT,
                venue TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                country_code TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'scheduled',
                home_sets INTEGER NOT NULL DEFAULT 0,
                away_sets INTEGER NOT NULL DEFAULT 0,
                set_scores_json TEXT NOT NULL DEFAULT '[]',
                source_url TEXT NOT NULL DEFAULT '',
                source_payload_json TEXT NOT NULL DEFAULT '{}',
                schedule_hash TEXT NOT NULL,
                first_seen_at_utc TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                UNIQUE (competition_key, match_code)
            );

            CREATE INDEX IF NOT EXISTS idx_matches_time
            ON matches (scheduled_at_utc);

            CREATE TABLE IF NOT EXISTS notification_log (
                dedupe_key TEXT PRIMARY KEY,
                notification_type TEXT NOT NULL,
                match_stable_key TEXT,
                sent_at_utc TEXT NOT NULL,
                delivery_state TEXT NOT NULL DEFAULT 'sent',
                claimed_at_utc TEXT
            );

            CREATE TABLE IF NOT EXISTS component_state (
                component TEXT PRIMARY KEY,
                last_success_at_utc TEXT,
                failure_count INTEGER NOT NULL DEFAULT 0,
                incident_fingerprint TEXT,
                incident_started_at_utc TEXT,
                last_error TEXT,
                alert_sent INTEGER NOT NULL DEFAULT 0,
                recovery_pending INTEGER NOT NULL DEFAULT 0,
                last_alert_at_utc TEXT
            );

            CREATE TABLE IF NOT EXISTS provider_sync_state (
                component TEXT PRIMARY KEY,
                last_attempt_at_utc TEXT,
                last_success_at_utc TEXT,
                next_due_at_utc TEXT,
                payload_hash TEXT,
                requests_date TEXT,
                requests_today INTEGER NOT NULL DEFAULT 0,
                quota_remaining INTEGER,
                last_record_count INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(matches)").fetchall()
        }
        if "provider" not in columns:
            conn.execute(
                "ALTER TABLE matches ADD COLUMN provider TEXT NOT NULL DEFAULT 'cev'"
            )
        if "next_result_check_at_utc" not in columns:
            conn.execute("ALTER TABLE matches ADD COLUMN next_result_check_at_utc TEXT")
        if "result_poll_attempts" not in columns:
            conn.execute(
                "ALTER TABLE matches ADD COLUMN result_poll_attempts INTEGER NOT NULL DEFAULT 0"
            )
        notification_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(notification_log)").fetchall()
        }
        if "delivery_state" not in notification_columns:
            conn.execute(
                "ALTER TABLE notification_log "
                "ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'sent'"
            )
        if "claimed_at_utc" not in notification_columns:
            conn.execute("ALTER TABLE notification_log ADD COLUMN claimed_at_utc TEXT")
        conn.commit()


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _schedule_hash(match: MatchRecord) -> str:
    relevant = {
        "home": match.home_team,
        "away": match.away_team,
        "scheduled": match.scheduled_at_utc,
        "timezone": match.local_timezone,
        "venue": match.venue,
        "phase": match.phase,
        "availability": match.status if match.status in {"cancelled", "postponed"} else "",
    }
    raw = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _row_to_match(row: sqlite3.Row | None) -> MatchRecord | None:
    if row is None:
        return None
    return MatchRecord(
        provider_match_id=row["provider_match_id"],
        competition_key=row["competition_key"],
        competition_id=row["competition_id"],
        competition_name=row["competition_name"],
        gender=row["gender"],
        match_code=row["match_code"],
        phase=row["phase"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        scheduled_at_utc=row["scheduled_at_utc"],
        local_timezone=row["local_timezone"],
        venue=row["venue"],
        city=row["city"],
        country_code=row["country_code"],
        status=row["status"],
        home_sets=row["home_sets"],
        away_sets=row["away_sets"],
        set_scores=[tuple(item) for item in json.loads(row["set_scores_json"] or "[]")],
        source_url=row["source_url"],
        source_payload=json.loads(row["source_payload_json"] or "{}"),
        provider=row["provider"],
        next_result_check_at_utc=row["next_result_check_at_utc"],
        result_poll_attempts=row["result_poll_attempts"],
        first_seen_at_utc=row["first_seen_at_utc"],
        updated_at_utc=row["updated_at_utc"],
    )


def get_match(db_path: str, competition_key: str, match_code: str) -> MatchRecord | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM matches WHERE competition_key = ? AND match_code = ?",
            (competition_key, match_code),
        ).fetchone()
    return _row_to_match(row)


def upsert_matches(
    db_path: str,
    matches: Iterable[MatchRecord],
    tracked_team: str,
) -> list[MatchChange]:
    changes: list[MatchChange] = []
    now = _now_iso()

    with _connect(db_path) as conn:
        for match in matches:
            row = conn.execute(
                "SELECT * FROM matches WHERE competition_key = ? AND match_code = ?",
                (match.competition_key, match.match_code),
            ).fetchone()
            previous = _row_to_match(row)

            # A partial provider record must never erase a previously resolved
            # time or venue. This is especially important for future CEV slots,
            # whose location can temporarily disappear while the bracket changes.
            if previous and match.scheduled_at_utc is None and previous.scheduled_at_utc:
                match.scheduled_at_utc = previous.scheduled_at_utc
                match.local_timezone = previous.local_timezone
            if previous and not match.venue and previous.venue:
                match.venue = previous.venue

            if previous and previous.status == "final" and match.status != "final":
                match.status = previous.status
                match.home_sets = previous.home_sets
                match.away_sets = previous.away_sets
                match.set_scores = previous.set_scores

            schedule_hash = _schedule_hash(match)
            is_new = previous is None
            became_tracked = bool(
                previous
                and not previous.involves(tracked_team)
                and match.involves(tracked_team)
            )
            schedule_changed = bool(
                previous
                and previous.involves(tracked_team)
                and match.involves(tracked_team)
                and _schedule_hash(previous) != schedule_hash
            )
            became_final = bool(
                match.status == "final" and (not previous or previous.status != "final")
            )

            payload = (
                match.provider_match_id,
                match.competition_key,
                match.competition_id,
                match.competition_name,
                match.gender,
                match.match_code,
                match.phase,
                match.home_team,
                match.away_team,
                match.scheduled_at_utc,
                match.local_timezone,
                match.venue,
                match.city,
                match.country_code,
                match.status,
                match.home_sets,
                match.away_sets,
                json.dumps(match.set_scores),
                match.source_url,
                json.dumps(match.source_payload, ensure_ascii=False),
                match.provider,
                match.next_result_check_at_utc,
                match.result_poll_attempts,
                schedule_hash,
                now,
                now,
            )
            conn.execute(
                """
                INSERT INTO matches (
                    provider_match_id, competition_key, competition_id,
                    competition_name, gender, match_code, phase,
                    home_team, away_team, scheduled_at_utc, local_timezone,
                    venue, city, country_code, status, home_sets, away_sets,
                    set_scores_json, source_url, source_payload_json,
                    provider, next_result_check_at_utc, result_poll_attempts,
                    schedule_hash, first_seen_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(competition_key, match_code) DO UPDATE SET
                    provider_match_id = excluded.provider_match_id,
                    competition_id = excluded.competition_id,
                    competition_name = excluded.competition_name,
                    gender = excluded.gender,
                    phase = excluded.phase,
                    home_team = excluded.home_team,
                    away_team = excluded.away_team,
                    scheduled_at_utc = excluded.scheduled_at_utc,
                    local_timezone = excluded.local_timezone,
                    venue = excluded.venue,
                    city = excluded.city,
                    country_code = excluded.country_code,
                    status = excluded.status,
                    home_sets = excluded.home_sets,
                    away_sets = excluded.away_sets,
                    set_scores_json = excluded.set_scores_json,
                    source_url = excluded.source_url,
                    source_payload_json = excluded.source_payload_json,
                    provider = excluded.provider,
                    schedule_hash = excluded.schedule_hash,
                    updated_at_utc = excluded.updated_at_utc
                """,
                payload,
            )
            changes.append(
                MatchChange(
                    match=match,
                    previous=previous,
                    is_new=is_new,
                    became_tracked=became_tracked,
                    schedule_changed=schedule_changed,
                    became_final=became_final,
                )
            )
        conn.commit()
    return changes


def list_matches(
    db_path: str,
    tracked_team: str | None = None,
    include_final: bool = True,
) -> list[MatchRecord]:
    query = "SELECT * FROM matches"
    clauses: list[str] = []
    params: list[Any] = []
    if tracked_team:
        clauses.append("(UPPER(home_team) = ? OR UPPER(away_team) = ?)")
        params.extend([tracked_team.upper(), tracked_team.upper()])
    if not include_final:
        clauses.append("status != 'final'")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY scheduled_at_utc IS NULL, scheduled_at_utc, competition_key, match_code"
    with _connect(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_match(row) for row in rows if row is not None]


def notification_sent(db_path: str, dedupe_key: str) -> bool:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM notification_log WHERE dedupe_key = ? AND delivery_state = 'sent'",
            (dedupe_key,),
        ).fetchone()
    return row is not None


def claim_notification_send(
    db_path: str,
    dedupe_key: str,
    notification_type: str,
    match_stable_key: str | None = None,
) -> bool:
    """Atomically reserve one notification delivery across bot processes."""
    now = _now_iso()
    stale_before = (dt.datetime.now(dt.timezone.utc) - NOTIFICATION_CLAIM_TIMEOUT).replace(
        microsecond=0
    ).isoformat()
    with _connect(db_path) as conn:
        claimed = conn.execute(
            """
            INSERT OR IGNORE INTO notification_log (
                dedupe_key, notification_type, match_stable_key, sent_at_utc,
                delivery_state, claimed_at_utc
            ) VALUES (?, ?, ?, ?, 'pending', ?)
            """,
            (dedupe_key, notification_type, match_stable_key, now, now),
        ).rowcount
        if claimed:
            return True

        # A crash after claiming must not suppress the notification forever.
        reclaimed = conn.execute(
            """
            UPDATE notification_log
            SET notification_type = ?, match_stable_key = ?, sent_at_utc = ?,
                delivery_state = 'pending', claimed_at_utc = ?
            WHERE dedupe_key = ?
              AND delivery_state = 'pending'
              AND claimed_at_utc < ?
            """,
            (notification_type, match_stable_key, now, now, dedupe_key, stale_before),
        ).rowcount
    return bool(reclaimed)


def mark_notification_sent(
    db_path: str,
    dedupe_key: str,
    notification_type: str,
    match_stable_key: str | None = None,
) -> None:
    with _connect(db_path) as conn:
        updated = conn.execute(
            """
            UPDATE notification_log
            SET notification_type = ?, match_stable_key = ?, sent_at_utc = ?,
                delivery_state = 'sent', claimed_at_utc = NULL
            WHERE dedupe_key = ?
            """,
            (notification_type, match_stable_key, _now_iso(), dedupe_key),
        ).rowcount
        if not updated:
            conn.execute(
                """
                INSERT OR IGNORE INTO notification_log
                    (dedupe_key, notification_type, match_stable_key, sent_at_utc)
                VALUES (?, ?, ?, ?)
                """,
                (dedupe_key, notification_type, match_stable_key, _now_iso()),
            )


def release_notification_claim(db_path: str, dedupe_key: str) -> None:
    """Make a failed Telegram delivery immediately eligible for retry."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM notification_log WHERE dedupe_key = ? AND delivery_state = 'pending'",
            (dedupe_key,),
        )


def get_component_state(db_path: str, component: str) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM component_state WHERE component = ?", (component,)
        ).fetchone()
    return dict(row) if row else None


def list_component_states(db_path: str) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM component_state ORDER BY component"
        ).fetchall()
    return [dict(row) for row in rows]


def record_component_failure(
    db_path: str,
    component: str,
    fingerprint: str,
    safe_error: str,
) -> dict[str, Any]:
    now = _now_iso()
    with _connect(db_path) as conn:
        current = conn.execute(
            "SELECT * FROM component_state WHERE component = ?", (component,)
        ).fetchone()
        # A component is still in the same outage until it records a success.
        # The fingerprint may change while the source moves between parse, HTTP,
        # and cache errors; that must not reset the consecutive-failure counter.
        ongoing_failure = bool(current and int(current["failure_count"]) > 0)
        count = int(current["failure_count"] if ongoing_failure else 0) + 1
        started = current["incident_started_at_utc"] if ongoing_failure else now
        alert_sent = int(current["alert_sent"] if ongoing_failure else 0)
        conn.execute(
            """
            INSERT INTO component_state (
                component, failure_count, incident_fingerprint,
                incident_started_at_utc, last_error, alert_sent
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(component) DO UPDATE SET
                failure_count = excluded.failure_count,
                incident_fingerprint = excluded.incident_fingerprint,
                incident_started_at_utc = excluded.incident_started_at_utc,
                last_error = excluded.last_error,
                alert_sent = excluded.alert_sent
            """,
            (component, count, fingerprint, started, safe_error, alert_sent),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM component_state WHERE component = ?", (component,)
        ).fetchone()
    return dict(row)


def mark_component_alert_sent(db_path: str, component: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE component_state
            SET alert_sent = 1, last_alert_at_utc = ?
            WHERE component = ?
            """,
            (_now_iso(), component),
        )
        conn.commit()


def record_component_success(db_path: str, component: str) -> dict[str, Any] | None:
    previous = get_component_state(db_path, component)
    recovery_pending = int(bool(previous and previous.get("alert_sent")))
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO component_state (
                component, last_success_at_utc, failure_count,
                incident_fingerprint, incident_started_at_utc,
                last_error, alert_sent, recovery_pending
            ) VALUES (?, ?, 0, NULL, NULL, NULL, 0, ?)
            ON CONFLICT(component) DO UPDATE SET
                last_success_at_utc = excluded.last_success_at_utc,
                failure_count = 0,
                incident_fingerprint = NULL,
                incident_started_at_utc = CASE
                    WHEN component_state.alert_sent = 1
                         OR component_state.recovery_pending = 1
                    THEN component_state.incident_started_at_utc
                    ELSE NULL
                END,
                last_error = NULL,
                alert_sent = 0,
                recovery_pending = MAX(component_state.recovery_pending, excluded.recovery_pending)
            """,
            (component, _now_iso(), recovery_pending),
        )
        conn.commit()
    return previous


def clear_recovery_pending(db_path: str, component: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE component_state
            SET recovery_pending = 0, incident_started_at_utc = NULL
            WHERE component = ?
            """,
            (component,),
        )
        conn.commit()


def notification_count(db_path: str) -> int:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM notification_log").fetchone()
    return int(row[0]) if row else 0


def update_result_poll_state(
    db_path: str,
    match: MatchRecord,
    *,
    next_check_at_utc: str | None,
    attempts: int,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            UPDATE matches
            SET next_result_check_at_utc = ?, result_poll_attempts = ?
            WHERE competition_key = ? AND match_code = ?
            """,
            (next_check_at_utc, attempts, match.competition_key, match.match_code),
        )
        conn.commit()


def get_provider_sync_state(db_path: str, component: str) -> dict[str, Any] | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM provider_sync_state WHERE component = ?", (component,)
        ).fetchone()
    return dict(row) if row else None


def record_provider_sync(
    db_path: str,
    component: str,
    *,
    success: bool,
    request_count: int = 0,
    quota_remaining: int | None = None,
    payload_hash: str | None = None,
    record_count: int = 0,
    next_due_at_utc: str | None = None,
) -> None:
    now = _now_iso()
    today = now[:10]
    with _connect(db_path) as conn:
        current = conn.execute(
            "SELECT * FROM provider_sync_state WHERE component = ?", (component,)
        ).fetchone()
        requests_today = request_count
        if current and current["requests_date"] == today:
            requests_today += int(current["requests_today"] or 0)
        last_success = now if success else (current["last_success_at_utc"] if current else None)
        stored_hash = payload_hash if payload_hash is not None else (
            current["payload_hash"] if current else None
        )
        stored_quota = quota_remaining if quota_remaining is not None else (
            current["quota_remaining"] if current else None
        )
        conn.execute(
            """
            INSERT INTO provider_sync_state (
                component, last_attempt_at_utc, last_success_at_utc,
                next_due_at_utc, payload_hash, requests_date,
                requests_today, quota_remaining, last_record_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(component) DO UPDATE SET
                last_attempt_at_utc = excluded.last_attempt_at_utc,
                last_success_at_utc = excluded.last_success_at_utc,
                next_due_at_utc = excluded.next_due_at_utc,
                payload_hash = excluded.payload_hash,
                requests_date = excluded.requests_date,
                requests_today = excluded.requests_today,
                quota_remaining = excluded.quota_remaining,
                last_record_count = excluded.last_record_count
            """,
            (
                component,
                now,
                last_success,
                next_due_at_utc,
                stored_hash,
                today,
                requests_today,
                stored_quota,
                record_count,
            ),
        )
        conn.commit()


def list_provider_sync_states(db_path: str) -> list[dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM provider_sync_state ORDER BY component"
        ).fetchall()
    return [dict(row) for row in rows]
