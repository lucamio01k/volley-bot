"""Provider registry shared by scheduler and CLI."""

from __future__ import annotations

from typing import Protocol

import config
from api_sports_client import APISportsClient
from cev_client import CEVClient
from models import CompetitionConfig, MatchRecord, ProviderBatch


class MatchProvider(Protocol):
    def fetch_schedule(self, competition: CompetitionConfig) -> ProviderBatch: ...

    def fetch_results(
        self, competition: CompetitionConfig, matches: list[MatchRecord]
    ) -> ProviderBatch: ...


def create_provider(competition: CompetitionConfig, timeout_seconds: int) -> MatchProvider:
    provider = competition.provider.strip().lower()
    if provider == "cev":
        return CEVClient(timeout_seconds=timeout_seconds)
    if provider == "api_sports":
        return APISportsClient(
            config.API_SPORTS_VOLLEYBALL_KEY,
            timeout_seconds=timeout_seconds,
        )
    raise ValueError(f"Unsupported match provider: {competition.provider}")
