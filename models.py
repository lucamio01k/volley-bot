"""Normalized domain models used by providers, storage and notifications."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class CompetitionConfig:
    key: str
    name: str
    gender: str
    competition_id: int
    active_from: str
    active_to: str


@dataclass
class MatchRecord:
    provider_match_id: str
    competition_key: str
    competition_id: int
    competition_name: str
    gender: str
    match_code: str
    phase: str
    home_team: str
    away_team: str
    scheduled_at_utc: str | None
    local_timezone: str | None
    venue: str
    city: str = ""
    country_code: str = ""
    status: str = "scheduled"
    home_sets: int = 0
    away_sets: int = 0
    set_scores: list[tuple[int, int]] = field(default_factory=list)
    source_url: str = ""
    source_payload: dict[str, Any] = field(default_factory=dict)
    first_seen_at_utc: str | None = None
    updated_at_utc: str | None = None

    @property
    def stable_key(self) -> str:
        return f"{self.competition_key}:{self.match_code}"

    def involves(self, team: str) -> bool:
        wanted = team.strip().upper()
        return self.home_team.upper() == wanted or self.away_team.upper() == wanted

    def opponent_of(self, team: str) -> str:
        wanted = team.strip().upper()
        if self.home_team.upper() == wanted:
            return self.away_team
        if self.away_team.upper() == wanted:
            return self.home_team
        return ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MatchChange:
    match: MatchRecord
    previous: MatchRecord | None
    is_new: bool = False
    became_tracked: bool = False
    schedule_changed: bool = False
