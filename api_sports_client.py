"""API-Sports Volleyball provider for automatic international friendlies."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from models import CompetitionConfig, MatchRecord, ProviderBatch

BASE_URL = "https://v1.volleyball.api-sports.io"
TERMINAL_STATUSES = {"FT", "AW"}
CANCELLED_STATUSES = {"CANC", "ABD"}
POSTPONED_STATUSES = {"POST", "INTR"}
LIVE_STATUSES = {"S1", "S2", "S3", "S4", "S5"}
SUPPORTED_STATUSES = {"NS", *TERMINAL_STATUSES, *CANCELLED_STATUSES, *POSTPONED_STATUSES, *LIVE_STATUSES}


class APISportsError(RuntimeError):
    """Safe provider error for operational alerts."""


class APISportsConfigError(APISportsError):
    """Raised when API-Sports is enabled without verified identifiers."""


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _team_name(team: dict[str, Any], tracked_team_id: int) -> str:
    if _integer(team.get("id")) == tracked_team_id:
        return "ITALY"
    return str(team.get("name") or "").strip().upper()


def _set_scores(game: dict[str, Any]) -> list[tuple[int, int]]:
    periods = game.get("periods")
    if not isinstance(periods, dict):
        return []
    result: list[tuple[int, int]] = []
    order = ("first", "second", "third", "fourth", "fifth", "set1", "set2", "set3", "set4", "set5")
    seen: set[str] = set()
    for key in order:
        value = periods.get(key)
        if key in seen or not isinstance(value, dict):
            continue
        home = value.get("home")
        away = value.get("away")
        if home is not None and away is not None:
            result.append((_integer(home), _integer(away)))
            seen.add(key)
    return result[:5]


def _scheduled_at_utc(game: dict[str, Any]) -> str | None:
    timestamp = game.get("timestamp")
    if timestamp is not None:
        try:
            return dt.datetime.fromtimestamp(int(timestamp), dt.timezone.utc).replace(
                microsecond=0
            ).isoformat()
        except (TypeError, ValueError, OSError):
            pass
    raw = str(game.get("date") or "").strip()
    if not raw:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_games_payload(
    payload: dict[str, Any], competition: CompetitionConfig
) -> tuple[list[MatchRecord], list[str]]:
    errors = payload.get("errors")
    if errors and errors != [] and errors != {}:
        raise APISportsError("API-Sports response contains errors")
    response = payload.get("response")
    if not isinstance(response, list):
        raise APISportsError("API-Sports response field is not a list")

    league_id = _integer(
        competition.provider_competition_id
        if competition.provider_competition_id is not None
        else competition.competition_id
    )
    team_id = _integer(competition.tracked_team_id)
    if league_id <= 0 or team_id <= 0 or not competition.season:
        raise APISportsConfigError(
            f"Unverified API-Sports identifiers for {competition.key}"
        )

    matches: list[MatchRecord] = []
    warnings: list[str] = []
    for game in response:
        if not isinstance(game, dict):
            warnings.append("API-Sports game record is not an object")
            continue
        league = game.get("league") if isinstance(game.get("league"), dict) else {}
        teams = game.get("teams") if isinstance(game.get("teams"), dict) else {}
        home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
        away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
        status_data = game.get("status") if isinstance(game.get("status"), dict) else {}
        status_short = str(status_data.get("short") or "").upper()
        game_id = _integer(game.get("id"))

        if _integer(league.get("id")) != league_id:
            continue
        if team_id not in {_integer(home.get("id")), _integer(away.get("id"))}:
            continue
        if status_short not in SUPPORTED_STATUSES:
            warnings.append(f"Unsupported API-Sports status for game {game_id}: {status_short}")
            continue

        home_name = _team_name(home, team_id)
        away_name = _team_name(away, team_id)
        scheduled = _scheduled_at_utc(game)
        if game_id <= 0 or not home_name or not away_name or not scheduled:
            warnings.append(f"Incomplete API-Sports game ignored: {game_id or 'missing id'}")
            continue

        scores = game.get("scores") if isinstance(game.get("scores"), dict) else {}
        venue_data = game.get("venue")
        venue = ""
        city = ""
        if isinstance(venue_data, dict):
            venue = str(venue_data.get("name") or "").strip()
            city = str(venue_data.get("city") or "").strip()
        elif isinstance(venue_data, str):
            venue = venue_data.strip()

        if status_short in TERMINAL_STATUSES:
            status = "final"
        elif status_short in CANCELLED_STATUSES:
            status = "cancelled"
        elif status_short in POSTPONED_STATUSES:
            status = "postponed"
        elif status_short in LIVE_STATUSES:
            status = "live"
        else:
            status = "scheduled"

        matches.append(
            MatchRecord(
                provider_match_id=str(game_id),
                competition_key=competition.key,
                competition_id=league_id,
                competition_name=competition.name,
                gender=competition.gender,
                match_code=f"game:{game_id}",
                phase=str(game.get("week") or league.get("name") or "Amichevole internazionale"),
                home_team=home_name,
                away_team=away_name,
                scheduled_at_utc=scheduled,
                local_timezone=str(game.get("timezone") or "UTC"),
                venue=venue,
                city=city,
                status=status,
                home_sets=_integer(scores.get("home")),
                away_sets=_integer(scores.get("away")),
                set_scores=_set_scores(game),
                source_url=f"{BASE_URL}/games?id={game_id}",
                source_payload=game,
                provider="api_sports",
            )
        )

    if len({match.stable_key for match in matches}) != len(matches):
        raise APISportsError("Duplicate API-Sports game identifiers")
    return matches, warnings


class APISportsClient:
    def __init__(self, api_key: str, timeout_seconds: int = 20) -> None:
        if not api_key.strip():
            raise APISportsConfigError("API_SPORTS_VOLLEYBALL_KEY is missing")
        self.timeout_seconds = timeout_seconds
        self.request_count = 0
        self.quota_remaining: int | None = None
        self.session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retry))
        self.session.headers.update({"x-apisports-key": api_key.strip()})

    def _get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        self.request_count += 1
        try:
            response = self.session.get(
                f"{BASE_URL}/{endpoint}", params=params, timeout=self.timeout_seconds
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise APISportsError(
                f"API-Sports HTTP request failed: {exc.__class__.__name__}"
            ) from exc
        remaining = response.headers.get("x-ratelimit-requests-remaining")
        if remaining is not None:
            self.quota_remaining = _integer(remaining)
        try:
            payload = response.json()
        except ValueError as exc:
            raise APISportsError("API-Sports returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise APISportsError("API-Sports payload is not an object")
        return payload

    def fetch_schedule(self, competition: CompetitionConfig) -> ProviderBatch:
        self.request_count = 0
        league_id = competition.provider_competition_id or competition.competition_id
        if not competition.season or not competition.tracked_team_id:
            raise APISportsConfigError(
                f"Season/team ID missing for {competition.key}"
            )
        payload = self._get(
            "games",
            {
                "league": league_id,
                "season": competition.season,
                "team": competition.tracked_team_id,
                "timezone": "UTC",
            },
        )
        matches, warnings = parse_games_payload(payload, competition)
        normalized = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return ProviderBatch(
            matches=matches,
            warnings=warnings,
            request_count=self.request_count,
            quota_remaining=self.quota_remaining,
            payload_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )

    def fetch_results(
        self,
        competition: CompetitionConfig,
        _matches: list[MatchRecord],
    ) -> ProviderBatch:
        return self.fetch_schedule(competition)

    def discover_identifiers(self) -> dict[str, Any]:
        """Return candidate friendly leagues and Italy teams without persisting them."""
        self.request_count = 0
        leagues = self._get("leagues", {"search": "Friendly International"})
        teams = self._get("teams", {"search": "Italy"})
        return {
            "leagues": leagues.get("response", []),
            "teams": teams.get("response", []),
            "requests": self.request_count,
            "quota_remaining": self.quota_remaining,
        }
