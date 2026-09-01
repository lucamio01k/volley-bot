"""Read EuroVolley schedules and match details from the official CEV site."""

from __future__ import annotations

import datetime as dt
import re
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from models import CompetitionConfig, MatchRecord

CEV_BASE_URL = "https://www-old.cev.eu/Competition-Area/"
COMPETITION_URL = urljoin(CEV_BASE_URL, "CompetitionView.aspx?ID={competition_id}")

VENUE_TIMEZONES = {
    "ISTANBUL": "Europe/Istanbul",
    "BRNO": "Europe/Prague",
    "BAKU": "Asia/Baku",
    "GOTHENBURG": "Europe/Stockholm",
    "NAPLES": "Europe/Rome",
    "MODENA": "Europe/Rome",
    "TORINO": "Europe/Rome",
    "ASSAGO": "Europe/Rome",
    "SOFIA": "Europe/Sofia",
    "TAMPERE": "Europe/Helsinki",
    "CLUJ NAPOCA": "Europe/Bucharest",
}

COUNTRY_TIMEZONES = {
    "AZE": "Asia/Baku",
    "BUL": "Europe/Sofia",
    "CZE": "Europe/Prague",
    "FIN": "Europe/Helsinki",
    "ITA": "Europe/Rome",
    "ROU": "Europe/Bucharest",
    "SWE": "Europe/Stockholm",
    "TUR": "Europe/Istanbul",
}


class CEVError(RuntimeError):
    """Base provider error safe to convert into an operational alert."""


class CEVParseError(CEVError):
    """Raised when the official HTML no longer matches required structure."""


def _clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split()).strip()


def _suffix_text(root: Tag, suffix: str) -> str:
    node = root.find(id=re.compile(re.escape(suffix) + r"$"))
    return _clean(node.get_text(" ", strip=True) if node else "")


def _timezone_hint(venue: str = "", phase: str = "") -> str | None:
    upper = venue.upper()
    for hint, timezone_name in VENUE_TIMEZONES.items():
        if hint in upper:
            return timezone_name

    phase_country = re.search(r"\bIN\s+([A-Z]{3})\b", phase.upper())
    if phase_country:
        return COUNTRY_TIMEZONES.get(phase_country.group(1))
    return None


def _parse_local_datetime(
    raw: str,
    venue: str,
    phase: str,
    match_label: str,
) -> tuple[str | None, str | None]:
    raw = _clean(raw)
    if not raw or raw == "---" or not re.search(r"\d", raw):
        return None, None
    try:
        local = dt.datetime.strptime(raw, "%d/%m/%Y %H:%M")
    except ValueError as exc:
        raise CEVParseError(f"Invalid CEV match datetime: {raw}") from exc
    timezone_name = _timezone_hint(venue, phase)
    if not timezone_name:
        raise CEVParseError(
            "Timezone unknown for match "
            f"{match_label}: venue={venue or 'missing venue'}, phase={phase or 'missing phase'}"
        )
    localized = local.replace(tzinfo=ZoneInfo(timezone_name))
    return localized.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat(), timezone_name


def _phase_views(soup: BeautifulSoup) -> list[tuple[str, Tag]]:
    tab_strip = soup.find(id=re.compile(r"RadTabStripChampionship$"))
    if tab_strip is None:
        raise CEVParseError("CEV championship tabs not found")
    titles = [
        _clean(item.get("title") or item.get_text(" ", strip=True))
        for item in tab_strip.select("a.rtsLink")
    ]
    container = soup.find(id=re.compile(r"RadMultiPageChampionship$"))
    if container is None:
        raise CEVParseError("CEV championship container not found")
    views = [
        item
        for item in container.find_all("div", recursive=False)
        if re.fullmatch(r"Content_Left_\d+", item.get("id", ""))
    ]
    if not titles or len(titles) != len(views):
        raise CEVParseError(
            f"CEV phase structure mismatch: {len(titles)} titles, {len(views)} views"
        )
    return list(zip(titles, views))


def parse_competition_html(html_text: str, competition: CompetitionConfig) -> list[MatchRecord]:
    if not html_text or len(html_text) < 1000:
        raise CEVParseError("CEV response is empty or unexpectedly short")

    soup = BeautifulSoup(html_text, "html.parser")
    matches: list[MatchRecord] = []

    for phase, view in _phase_views(soup):
        view_match = re.search(r"Content_Left_(\d+)$", view.get("id", ""))
        if not view_match:
            raise CEVParseError(f"Invalid CEV phase view ID for {phase}")
        phase_code = view_match.group(1)
        blocks = view.select('div[id$="_div_match"]')
        for slot_number, block in enumerate(blocks, start=1):
            federation_match_code = _suffix_text(block, "LB_FederationMatchNumber")
            slot_code = f"{phase_code}:{slot_number:03d}"
            home_team = _suffix_text(block, "Label2")
            away_team = _suffix_text(block, "Label4")
            raw_date = _suffix_text(block, "LB_DataOra")
            venue = _suffix_text(block, "LB_Palasport")
            home_score_raw = _suffix_text(block, "LB_SetCasa")
            away_score_raw = _suffix_text(block, "LB_SetOspiti")

            link = block.find("a", href=re.compile(r"MatchPage\.aspx\?mID="))
            if not link or not federation_match_code or not home_team or not away_team:
                raise CEVParseError(
                    "Required fields missing in CEV match block "
                    f"({federation_match_code or slot_code})"
                )

            source_url = urljoin(CEV_BASE_URL, str(link.get("href")))
            query = parse_qs(urlparse(source_url).query)
            match_id = (query.get("mID") or [""])[0]
            if not match_id.isdigit():
                raise CEVParseError(f"Invalid CEV match ID for {federation_match_code}")

            scheduled_at_utc, timezone_name = _parse_local_datetime(
                raw_date,
                venue,
                phase,
                federation_match_code or slot_code,
            )
            home_sets = int(home_score_raw) if home_score_raw.isdigit() else 0
            away_sets = int(away_score_raw) if away_score_raw.isdigit() else 0
            result_available = max(home_sets, away_sets) == 3 and min(
                home_sets, away_sets
            ) <= 2

            matches.append(
                MatchRecord(
                    provider_match_id=match_id,
                    competition_key=competition.key,
                    competition_id=competition.competition_id,
                    competition_name=competition.name,
                    gender=competition.gender,
                    match_code=slot_code,
                    phase=phase,
                    home_team=home_team,
                    away_team=away_team,
                    scheduled_at_utc=scheduled_at_utc,
                    local_timezone=timezone_name,
                    venue=venue,
                    status="result_available" if result_available else "scheduled",
                    home_sets=home_sets,
                    away_sets=away_sets,
                    source_url=source_url,
                    source_payload={
                        "federation_match_code": federation_match_code,
                        "phase_code": phase_code,
                        "slot_number": slot_number,
                        "raw_local_datetime": raw_date,
                    },
                )
            )

    if len(matches) < 10:
        raise CEVParseError(f"Only {len(matches)} matches recognized; refusing unsafe import")
    if len({match.stable_key for match in matches}) != len(matches):
        raise CEVParseError("Duplicate CEV match codes found in competition")
    return matches


def parse_match_detail_html(html_text: str, match: MatchRecord) -> MatchRecord:
    if not html_text or len(html_text) < 1000:
        raise CEVParseError("CEV match detail is empty or unexpectedly short")
    soup = BeautifulSoup(html_text, "html.parser")

    def text_by_id(node_id: str) -> str:
        node = soup.find(id=node_id)
        return _clean(node.get_text(" ", strip=True) if node else "")

    home = text_by_id("Content_Left_LB_Casa")
    away = text_by_id("Content_Left_LB_Ospiti")
    home_sets_raw = text_by_id("Content_Left_LB_SetCasa")
    away_sets_raw = text_by_id("Content_Left_LB_SetOspiti")
    if not home or not away or not home_sets_raw.isdigit() or not away_sets_raw.isdigit():
        raise CEVParseError(f"Invalid match detail header for {match.stable_key}")

    set_scores: list[tuple[int, int]] = []
    for number in range(1, 6):
        home_raw = text_by_id(f"Content_Left_LB_Set{number}Casa")
        away_raw = text_by_id(f"Content_Left_LB_Set{number}Ospiti")
        if home_raw.isdigit() and away_raw.isdigit():
            set_scores.append((int(home_raw), int(away_raw)))

    home_sets = int(home_sets_raw)
    away_sets = int(away_sets_raw)
    is_final_score = max(home_sets, away_sets) == 3 and min(home_sets, away_sets) <= 2
    is_complete = is_final_score and len(set_scores) == home_sets + away_sets

    date_raw = text_by_id("Content_Right_MatchInfoBox1_L_MatchDate")
    time_raw = text_by_id("Content_Right_MatchInfoBox1_L_MatchHour")
    venue = text_by_id("Content_Right_MatchInfoBox1_L_Impianto") or match.venue
    city = text_by_id("Content_Right_MatchInfoBox1_L_Citta")
    country_raw = text_by_id("Content_Right_MatchInfoBox1_L_Nazione")
    country_match = re.search(r"([A-Z]{3})", country_raw.upper())
    country_code = country_match.group(1) if country_match else ""

    timezone_name = (
        COUNTRY_TIMEZONES.get(country_code)
        or _timezone_hint(venue, match.phase)
        or match.local_timezone
    )
    if not timezone_name:
        raise CEVParseError(f"Timezone unavailable in match detail for {match.stable_key}")
    try:
        ZoneInfo(timezone_name)
    except (KeyError, ValueError) as exc:
        raise CEVParseError(
            f"Invalid timezone {timezone_name} in match detail for {match.stable_key}"
        ) from exc

    scheduled_at_utc = match.scheduled_at_utc
    if date_raw and time_raw:
        try:
            local = dt.datetime.strptime(f"{date_raw} {time_raw}", "%d/%m/%Y %H:%M")
        except ValueError as exc:
            raise CEVParseError(f"Invalid detail datetime for {match.stable_key}") from exc
        scheduled_at_utc = (
            local.replace(tzinfo=ZoneInfo(timezone_name))
            .astimezone(dt.timezone.utc)
            .replace(microsecond=0)
            .isoformat()
        )

    match.home_team = home
    match.away_team = away
    match.home_sets = home_sets
    match.away_sets = away_sets
    match.set_scores = set_scores
    match.status = "final" if is_complete else "scheduled"
    match.scheduled_at_utc = scheduled_at_utc
    match.local_timezone = timezone_name
    match.venue = venue
    match.city = city
    match.country_code = country_code
    return match


class CEVClient:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=0.8,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.headers.update(
            {
                "User-Agent": "ItaliaVolleyTelegramBot/1.0 (+personal non-commercial notifier)",
                "Accept": "text/html,application/xhtml+xml",
            }
        )

    def _get(self, url: str) -> str:
        try:
            response = self.session.get(url, timeout=self.timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise CEVError(f"CEV HTTP request failed: {exc.__class__.__name__}") from exc
        response.encoding = response.apparent_encoding or response.encoding or "utf-8"
        return response.text

    def fetch_competition(self, competition: CompetitionConfig) -> list[MatchRecord]:
        url = COMPETITION_URL.format(competition_id=competition.competition_id)
        return parse_competition_html(self._get(url), competition)

    def fetch_match_detail(self, match: MatchRecord) -> MatchRecord:
        if not match.source_url:
            raise CEVParseError(f"Source URL missing for {match.stable_key}")
        return parse_match_detail_html(self._get(match.source_url), match)
