"""Müllabfuhr-Termine: aus dem Abfallkalender der Gemeinde (iCal/ICS) oder aus festen Regeln.

Fast jede Gemeinde bzw. jeder Abfallwirtschaftsbetrieb bietet den Abfuhrkalender als iCal-Datei
oder Abo-Link an. Die Einträge werden über Stichwörter den Tonnen zugeordnet
(z. B. „Gelber Sack“ → gelb, „Papiertonne“ → papier, „Restmüll“ → rest).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class WasteType:
    key: str
    name: str
    color: str
    keywords: tuple[str, ...]


DEFAULT_TYPES = [
    WasteType("gelb", "Gelber Sack", "#f2c200", ("gelb", "wertstoff", "leichtverpackung", "lvp", "verpackung")),
    WasteType("papier", "Papier", "#2f6fd6", ("papier", "pappe", "karton", "ppk", "blaue")),
    WasteType("rest", "Restmüll", "#5b6270", ("rest", "hausmüll", "hausmuell", "graue", "schwarze")),
]


@dataclass
class WasteRule:
    """Regelmäßige Abfuhr ohne Kalenderdatei: erster Termin + Rhythmus in Wochen."""

    type: str
    start: date
    every_weeks: int = 2
    moved: dict[date, date | None] = field(default_factory=dict)  # Feiertagsverschiebungen

    def dates(self, since: date, until: date) -> list[date]:
        out: list[date] = []
        step = timedelta(weeks=max(1, self.every_weeks))
        d = self.start
        if d < since:
            d += step * ((since - d) // step)
        while d <= until + timedelta(days=7):
            out.append(d)
            d += step
        result = []
        for d in out:
            if d in self.moved:
                new = self.moved[d]
                if new is None:
                    continue
                d = new
            if since <= d <= until:
                result.append(d)
        return sorted(set(result))


# --- ICS ------------------------------------------------------------------------------------------


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_dt(value: str) -> date:
    value = value.strip()
    d = datetime.strptime(value[:8], "%Y%m%d").date()
    return d


def _unescape(value: str) -> str:
    return value.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _expand_rrule(start: date, rule: str, until_limit: date) -> list[date]:
    """Unterstützt die in Abfallkalendern üblichen Wiederholungen (DAILY/WEEKLY/MONTHLY mit INTERVAL, COUNT, UNTIL)."""
    parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
    freq = parts.get("FREQ", "").upper()
    interval = max(1, int(parts.get("INTERVAL", "1")))
    count = int(parts["COUNT"]) if "COUNT" in parts else None
    until = _parse_dt(parts["UNTIL"]) if "UNTIL" in parts else None
    end = min(until, until_limit) if until else until_limit
    byday = [_WEEKDAYS[d[-2:]] for d in parts.get("BYDAY", "").split(",") if d[-2:] in _WEEKDAYS]
    out: list[date] = []
    i = 0
    if freq == "WEEKLY" and byday:
        week0 = start - timedelta(days=start.weekday())
        while True:
            week = week0 + timedelta(weeks=i * interval)
            if week > end:
                break
            for wd in sorted(byday):
                d = week + timedelta(days=wd)
                if d < start or d > end:
                    continue
                out.append(d)
                if count and len(out) >= count:
                    return out
            i += 1
            if i > 5000:
                break
        return out
    while True:
        if freq == "DAILY":
            d = start + timedelta(days=i * interval)
        elif freq == "WEEKLY":
            d = start + timedelta(weeks=i * interval)
        elif freq == "MONTHLY":
            m = start.month - 1 + i * interval
            y, m = start.year + m // 12, m % 12 + 1
            try:
                d = start.replace(year=y, month=m)
            except ValueError:  # z. B. 31. eines kürzeren Monats
                i += 1
                continue
        elif freq == "YEARLY":
            try:
                d = start.replace(year=start.year + i * interval)
            except ValueError:
                i += 1
                continue
        else:
            return [start]
        if d > end:
            break
        out.append(d)
        i += 1
        if (count and len(out) >= count) or i > 5000:
            break
    return out


def parse_ics(text: str, until: date) -> list[tuple[date, str]]:
    """Liefert (Datum, Titel) aller Termine bis ``until``."""
    events: list[tuple[date, str]] = []
    cur: dict[str, Any] | None = None
    for line in _unfold(text):
        if line == "BEGIN:VEVENT":
            cur = {"exdates": set()}
            continue
        if line == "END:VEVENT":
            if cur and "start" in cur:
                summary = cur.get("summary", "") + " " + cur.get("description", "")
                dates = _expand_rrule(cur["start"], cur["rrule"], until) if "rrule" in cur else [cur["start"]]
                for d in dates:
                    if d not in cur["exdates"]:
                        events.append((d, summary.strip()))
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.split(";", 1)[0].upper()
        if key == "DTSTART":
            try:
                cur["start"] = _parse_dt(value)
            except ValueError:
                pass
        elif key == "SUMMARY":
            cur["summary"] = _unescape(value)
        elif key == "DESCRIPTION":
            cur["description"] = _unescape(value)[:200]
        elif key == "RRULE":
            cur["rrule"] = value.strip()
        elif key == "EXDATE":
            for v in value.split(","):
                try:
                    cur["exdates"].add(_parse_dt(v))
                except ValueError:
                    pass
    return events


def classify(title: str, types: list[WasteType]) -> str | None:
    t = title.lower()
    for wt in types:
        if any(k in t for k in wt.keywords):
            return wt.key
    return None


# --- Zusammenfassung ------------------------------------------------------------------------------


def _human(d: date, today: date) -> str:
    days = (d - today).days
    if days == 0:
        return "Heute"
    if days == 1:
        return "Morgen"
    if days == 2:
        return "Übermorgen"
    names = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
    return f"{names[d.weekday()]} {d.day:02d}.{d.month:02d}."


def summarize(
    dates_by_type: dict[str, list[date]], types: list[WasteType], today: date, upcoming: int = 4
) -> dict[str, Any]:
    bins = []
    for wt in types:
        future = sorted(d for d in dates_by_type.get(wt.key, []) if d >= today)
        nxt = future[0] if future else None
        bins.append(
            {
                "key": wt.key,
                "name": wt.name,
                "color": wt.color,
                "next": nxt.isoformat() if nxt else None,
                "in_days": (nxt - today).days if nxt else None,
                "label": _human(nxt, today) if nxt else "kein Termin",
                "upcoming": [d.isoformat() for d in future[:upcoming]],
            }
        )
    tomorrow = [b["name"] for b in bins if b["in_days"] == 1]
    today_ = [b["name"] for b in bins if b["in_days"] == 0]
    reminder = None
    if tomorrow:
        reminder = f"Heute Abend rausstellen: {', '.join(tomorrow)}"
    elif today_:
        reminder = f"Heute Abholung: {', '.join(today_)}"
    return {"bins": bins, "reminder": reminder}


class WasteCalendar:
    """Lädt die Termine (ICS-Link, ICS-Datei oder Regeln) und hält sie zwischengespeichert."""

    def __init__(
        self,
        types: list[WasteType] | None = None,
        ics_urls: list[str] | None = None,
        ics_files: list[Path] | None = None,
        rules: list[WasteRule] | None = None,
    ):
        self.types = types or list(DEFAULT_TYPES)
        self.ics_urls = ics_urls or []
        self.ics_files = ics_files or []
        self.rules = rules or []
        self.dates: dict[str, list[date]] = {}
        self.error: str | None = None
        self.loaded_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return bool(self.ics_urls or self.ics_files or self.rules)

    def _add_ics(self, text: str, acc: dict[str, set[date]], until: date) -> None:
        for d, title in parse_ics(text, until):
            key = classify(title, self.types)
            if key:
                acc.setdefault(key, set()).add(d)

    async def refresh(self, session: Any | None, today: date) -> None:
        until = today + timedelta(days=400)
        acc: dict[str, set[date]] = {}
        errors = []
        for rule in self.rules:
            acc.setdefault(rule.type, set()).update(rule.dates(today - timedelta(days=7), until))
        for path in self.ics_files:
            try:
                self._add_ics(Path(path).read_text(encoding="utf-8", errors="replace"), acc, until)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        for url in self.ics_urls:
            if session is None:
                break
            try:
                url = re.sub(r"^webcal://", "https://", url)
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    self._add_ics(await resp.text(errors="replace"), acc, until)
            except Exception as exc:  # Netz weg: alte Termine behalten
                errors.append(f"Abfallkalender nicht ladbar: {exc}")
        if acc or not errors:
            self.dates = {k: sorted(v) for k, v in acc.items()}
        self.error = "; ".join(errors) or None
        self.loaded_at = datetime.now().astimezone()

    def summary(self, today: date) -> dict[str, Any]:
        out = summarize(self.dates, self.types, today)
        out["configured"] = self.configured
        out["error"] = self.error
        return out
