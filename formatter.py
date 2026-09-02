"""Telegram HTML formatting for schedules, reminders, results and alerts."""

from __future__ import annotations

import datetime as dt
import html
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from models import MatchRecord

TEAM_FLAGS = {
    "AUSTRIA": "🇦🇹",
    "AZERBAIJAN": "🇦🇿",
    "BELGIUM": "🇧🇪",
    "BULGARIA": "🇧🇬",
    "CROATIA": "🇭🇷",
    "CZECHIA": "🇨🇿",
    "FRANCE": "🇫🇷",
    "GERMANY": "🇩🇪",
    "GREECE": "🇬🇷",
    "HUNGARY": "🇭🇺",
    "ITALY": "🇮🇹",
    "LATVIA": "🇱🇻",
    "MONTENEGRO": "🇲🇪",
    "POLAND": "🇵🇱",
    "PORTUGAL": "🇵🇹",
    "ROMANIA": "🇷🇴",
    "SERBIA": "🇷🇸",
    "SLOVAKIA": "🇸🇰",
    "SLOVENIA": "🇸🇮",
    "SPAIN": "🇪🇸",
    "SWEDEN": "🇸🇪",
    "THE NETHERLANDS": "🇳🇱",
    "TÜRKIYE": "🇹🇷",
    "TURKIYE": "🇹🇷",
    "UKRAINE": "🇺🇦",
}

WEEKDAYS = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MONTHS = [
    "",
    "gen",
    "feb",
    "mar",
    "apr",
    "mag",
    "giu",
    "lug",
    "ago",
    "set",
    "ott",
    "nov",
    "dic",
]


def _gender_label(gender: str) -> str:
    return "Femminile" if gender.lower() == "women" else "Maschile"


def _team(team: str) -> str:
    flag = TEAM_FLAGS.get(team.upper(), "🏳️")
    return f"{flag} {html.escape(team.title())}"


def _date(match: MatchRecord, timezone_name: str) -> dt.datetime | None:
    if not match.scheduled_at_utc:
        return None
    parsed = dt.datetime.fromisoformat(match.scheduled_at_utc.replace("Z", "+00:00"))
    return parsed.astimezone(ZoneInfo(timezone_name))


def _calendar_link(match: MatchRecord) -> str:
    """Build a Google Calendar link for a scheduled match lasting 90 minutes."""
    if not match.scheduled_at_utc or match.status == "cancelled":
        return ""
    try:
        start = dt.datetime.fromisoformat(match.scheduled_at_utc.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.timezone.utc)
    start = start.astimezone(dt.timezone.utc)
    end = start + dt.timedelta(minutes=90)
    location = ", ".join(item for item in (match.venue, match.city) if item)
    home_flag = TEAM_FLAGS.get(match.home_team.upper(), "🏳️")
    away_flag = TEAM_FLAGS.get(match.away_team.upper(), "🏳️")
    params = {
        "action": "TEMPLATE",
        "text": (
            f"🏐 {home_flag} {match.home_team.title()} vs "
            f"{away_flag} {match.away_team.title()}"
        ),
        "dates": f"{start:%Y%m%dT%H%M%SZ}/{end:%Y%m%dT%H%M%SZ}",
        "details": f"{match.competition_name} · {_phase(match.phase)}",
    }
    if location:
        params["location"] = location
    url = "https://calendar.google.com/calendar/render?" + urlencode(params)
    return f'<a href="{html.escape(url, quote=True)}">📅 Aggiungi al calendario</a>'


def _phase(phase: str) -> str:
    lowered = phase.lower()
    if "eight final" in lowered:
        return "Ottavi di finale"
    if "quarter" in lowered:
        return "Quarti di finale"
    if "semifinal" in lowered:
        return "Semifinale"
    if "bronze" in lowered:
        return "Finale 3° posto"
    if "gold" in lowered:
        return "Finale"
    if "pool" in lowered:
        return "Fase a gironi"
    return phase


def format_match_line(match: MatchRecord, timezone_name: str) -> str:
    local = _date(match, timezone_name)
    when = "data da definire"
    if local:
        when = f"{WEEKDAYS[local.weekday()]} {local.day} {MONTHS[local.month]}, {local:%H:%M}"
    venue = f" · {html.escape(match.venue)}" if match.venue else ""
    lines = [
        f"<b>{_gender_label(match.gender)}</b> · {html.escape(_phase(match.phase))}",
        f"{_team(match.home_team)} – {_team(match.away_team)}",
        f"🕒 {when}{venue}",
    ]
    calendar_link = _calendar_link(match)
    if calendar_link:
        lines.append(calendar_link)
    return "\n".join(lines)


def format_weekly(matches: list[MatchRecord], start: dt.date, end: dt.date, timezone_name: str) -> str:
    lines = [
        "<b>🏐 Italia Volley — questa settimana</b>",
        f"<i>{start.day} {MONTHS[start.month]} – {end.day} {MONTHS[end.month]}</i>",
        "",
    ]
    if not matches:
        lines.append("Nessuna partita delle nazionali senior in programma.")
    else:
        for index, match in enumerate(matches):
            if index:
                lines.append("")
            lines.append(format_match_line(match, timezone_name))
    return "\n".join(lines)


def format_daily(matches: list[MatchRecord], date: dt.date, timezone_name: str) -> str:
    lines = [f"<b>🏐 Italia Volley — oggi, {date.day} {MONTHS[date.month]}</b>", ""]
    if not matches:
        lines.append("Nessuna partita in programma oggi.")
    else:
        for index, match in enumerate(matches):
            if index:
                lines.append("")
            lines.append(format_match_line(match, timezone_name))
    return "\n".join(lines)


def format_reminder(match: MatchRecord, hours_before: int, timezone_name: str) -> str:
    return (
        f"<b>⏰ Tra {hours_before} ore gioca l’Italia</b>\n\n"
        f"{format_match_line(match, timezone_name)}"
    )


def format_new_fixture(match: MatchRecord, timezone_name: str) -> str:
    return f"<b>🆕 Nuova partita confermata</b>\n\n{format_match_line(match, timezone_name)}"


def format_schedule_change(match: MatchRecord, timezone_name: str) -> str:
    if match.status == "cancelled":
        return f"<b>❌ Partita annullata</b>\n\n{format_match_line(match, timezone_name)}"
    if match.status == "postponed":
        return f"<b>⏸ Partita rinviata o interrotta</b>\n\n{format_match_line(match, timezone_name)}"
    return f"<b>🔄 Aggiornamento calendario</b>\n\n{format_match_line(match, timezone_name)}"


def format_result(match: MatchRecord) -> str:
    sets = " · ".join(f"{home}-{away}" for home, away in match.set_scores)
    lines = [
        f"<b>✅ {html.escape(match.competition_name)} {_gender_label(match.gender)} — risultato</b>",
        f"{_team(match.home_team)} <b>{match.home_sets}–{match.away_sets}</b> {_team(match.away_team)}",
    ]
    if sets:
        lines.append(f"<i>Parziali: {sets}</i>")
    return "\n".join(lines)


def format_operational_error(
    component: str,
    safe_error: str,
    attempts: int,
    last_success: str | None,
) -> str:
    last = html.escape(last_success or "mai")
    return (
        "<b>⚠️ Errore bot volley</b>\n"
        f"Componente: <code>{html.escape(component)}</code>\n"
        f"Problema: {html.escape(safe_error)}\n"
        f"Tentativi consecutivi: {attempts}\n"
        f"Ultimo aggiornamento valido: {last}\n"
        "Il bot conserva l’ultimo cache valido e continuerà a riprovare."
    )


def format_recovery(component: str, duration: str) -> str:
    return (
        "<b>✅ Bot volley ripristinato</b>\n"
        f"Componente: <code>{html.escape(component)}</code>\n"
        f"Durata approssimativa: {html.escape(duration)}"
    )
