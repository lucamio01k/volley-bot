"""SQLite persistence and idempotency helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
from typing import Any, Iterable

from models import MatchChange, MatchRecord


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
                sent_at_utc TEXT NOT NULL
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
            """
        )
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
                    schedule_hash, first_seen_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "SELECT 1 FROM notification_log WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
    return row is not None


def mark_notification_sent(
    db_path: str,
    dedupe_key: str,
    notification_type: str,
    match_stable_key: str | None = None,
) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO notification_log
                (dedupe_key, notification_type, match_stable_key, sent_at_utc)
            VALUES (?, ?, ?, ?)
            """,
            (dedupe_key, notification_type, match_stable_key, _now_iso()),
        )
        conn.commit()


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
