"""PV-Ertragsprognose über Open-Meteo (kostenlos, ohne Schlüssel).

Open-Meteo liefert die stündliche Einstrahlung auf eine geneigte Fläche (``global_tilted_irradiance``)
für Neigung und Ausrichtung der Module. Daraus wird die Leistung geschätzt:

    P = kWp · GTI / 1000 · Systemfaktor · Temperaturfaktor, begrenzt auf die Wechselrichterleistung

Open-Meteo-Azimut: 0 = Süden, −90 = Osten, 90 = Westen.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

OPEN_METEO = "https://api.open-meteo.com/v1/forecast"

WEATHER = {
    0: ("Klar", "☀️"), 1: ("Überwiegend klar", "🌤️"), 2: ("Teils bewölkt", "⛅"), 3: ("Bedeckt", "☁️"),
    45: ("Nebel", "🌫️"), 48: ("Reifnebel", "🌫️"), 51: ("Nieselregen", "🌦️"), 53: ("Nieselregen", "🌦️"),
    55: ("Nieselregen", "🌧️"), 56: ("Gefrierender Niesel", "🌧️"), 57: ("Gefrierender Niesel", "🌧️"),
    61: ("Leichter Regen", "🌦️"), 63: ("Regen", "🌧️"), 65: ("Starker Regen", "🌧️"),
    66: ("Eisregen", "🌧️"), 67: ("Eisregen", "🌧️"), 71: ("Leichter Schnee", "🌨️"), 73: ("Schnee", "🌨️"),
    75: ("Starker Schnee", "❄️"), 77: ("Schneegriesel", "🌨️"), 80: ("Schauer", "🌦️"), 81: ("Schauer", "🌧️"),
    82: ("Starke Schauer", "🌧️"), 85: ("Schneeschauer", "🌨️"), 86: ("Schneeschauer", "🌨️"),
    95: ("Gewitter", "⛈️"), 96: ("Gewitter mit Hagel", "⛈️"), 99: ("Gewitter mit Hagel", "⛈️"),
}


@dataclass
class Plane:
    """Eine Modulfläche (z. B. PV1 und PV2 am PowerStream mit unterschiedlicher Ausrichtung)."""

    kwp: float
    tilt: float = 30.0
    azimuth: float = 0.0  # 0 = Süd, −90 = Ost, 90 = West
    name: str = ""


@dataclass
class ForecastConfig:
    latitude: float
    longitude: float
    planes: list[Plane] = field(default_factory=list)
    system_factor: float = 0.85  # Verluste: Kabel, Wechselrichter, Verschmutzung
    max_ac_w: float = 800.0  # z. B. PowerStream 600 oder 800 W


def plane_power(kwp: float, gti: float, temp_c: float | None, system_factor: float) -> float:
    if gti <= 0:
        return 0.0
    t_cell = (temp_c if temp_c is not None else 15.0) + gti / 800 * 25  # einfache Zelltemperatur
    temp_factor = 1 - 0.004 * (t_cell - 25)
    return kwp * 1000 * gti / 1000 * system_factor * max(0.7, min(1.1, temp_factor))


def build_forecast(
    cfg: ForecastConfig, responses: list[dict[str, Any]], now: datetime
) -> dict[str, Any]:
    """Kombiniert die Open-Meteo-Antworten (eine je Fläche) zu Stundenwerten und Tagessummen."""
    hourly: dict[str, float] = {}
    for plane, resp in zip(cfg.planes, responses):
        h = resp.get("hourly", {})
        times, gti, temp = h.get("time", []), h.get("global_tilted_irradiance", []), h.get("temperature_2m", [])
        for i, t in enumerate(times):
            g = gti[i] if i < len(gti) and gti[i] is not None else 0.0
            tc = temp[i] if i < len(temp) else None
            hourly[t] = hourly.get(t, 0.0) + plane_power(plane.kwp, g, tc, cfg.system_factor)

    points = []
    days: dict[str, float] = {}
    for t in sorted(hourly):
        w = min(hourly[t], cfg.max_ac_w)
        # Open-Meteo-Strahlungswerte sind Mittel der vorangegangenen Stunde → Mitte der Stunde anzeigen.
        mid = datetime.fromisoformat(t) - timedelta(minutes=30)
        points.append({"ts": mid.isoformat(timespec="minutes"), "w": round(w)})
        days[mid.date().isoformat()] = days.get(mid.date().isoformat(), 0.0) + w  # 1 h · W = Wh
    today = now.date()
    local_now = now.replace(tzinfo=None)
    rest_today = sum(
        p["w"] for p in points
        if datetime.fromisoformat(p["ts"]).date() == today and datetime.fromisoformat(p["ts"]) >= local_now
    )
    nearest = min(points, key=lambda p: abs(datetime.fromisoformat(p["ts"]) - local_now), default=None)
    first = responses[0] if responses else {}
    current = first.get("current", {})
    code = current.get("weather_code")
    daily = first.get("daily", {})
    weather_days = []
    for i, d in enumerate(daily.get("time", [])[:3]):
        c = daily.get("weather_code", [None] * 3)[i]
        weather_days.append(
            {
                "date": d,
                "kwh": round(days.get(d, 0.0) / 1000, 2),
                "t_max": daily.get("temperature_2m_max", [None] * 3)[i],
                "t_min": daily.get("temperature_2m_min", [None] * 3)[i],
                "icon": WEATHER.get(c, ("", "❔"))[1],
                "text": WEATHER.get(c, ("", ""))[0],
                "sun_h": round((daily.get("sunshine_duration", [0] * 3)[i] or 0) / 3600, 1),
            }
        )
    return {
        "points": points,
        "today_kwh": round(days.get(today.isoformat(), 0.0) / 1000, 2),
        "tomorrow_kwh": round(days.get((today + timedelta(days=1)).isoformat(), 0.0) / 1000, 2),
        "rest_today_kwh": round(rest_today / 1000, 2),
        "now_w": nearest["w"] if nearest else None,
        "days": weather_days,
        "weather": {
            "temp_c": current.get("temperature_2m"),
            "text": WEATHER.get(code, ("", ""))[0],
            "icon": WEATHER.get(code, ("", "❔"))[1],
        } if current else None,
    }


class SolarForecast:
    def __init__(self, cfg: ForecastConfig, url: str = OPEN_METEO):
        self.cfg = cfg
        self.url = url
        self.data: dict[str, Any] | None = None
        self.error: str | None = None
        self.loaded_at: datetime | None = None

    def _params(self, plane: Plane) -> dict[str, str]:
        return {
            "latitude": f"{self.cfg.latitude:.4f}",
            "longitude": f"{self.cfg.longitude:.4f}",
            "hourly": "global_tilted_irradiance,temperature_2m",
            "current": "temperature_2m,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunshine_duration",
            "tilt": f"{plane.tilt:g}",
            "azimuth": f"{plane.azimuth:g}",
            "timezone": "auto",
            "forecast_days": "3",
        }

    async def refresh(self, session: Any, now: datetime) -> None:
        try:
            responses = []
            for plane in self.cfg.planes:
                async with session.get(self.url, params=self._params(plane)) as resp:
                    resp.raise_for_status()
                    responses.append(await resp.json(content_type=None))
            self.data = build_forecast(self.cfg, responses, now)
            self.error = None
            self.loaded_at = now
        except Exception as exc:
            self.error = f"Wetterprognose nicht ladbar: {exc}"

    def recompute(self, now: datetime) -> None:
        """„Rest heute“ und „jetzt“ ändern sich mit der Uhrzeit, auch ohne neue Abfrage."""
        if not self.data:
            return
        pts = self.data["points"]
        local_now = now.replace(tzinfo=None)
        self.data["rest_today_kwh"] = round(sum(
            p["w"] for p in pts
            if datetime.fromisoformat(p["ts"]).date() == local_now.date() and datetime.fromisoformat(p["ts"]) >= local_now
        ) / 1000, 2)
        nearest = min(pts, key=lambda p: abs(datetime.fromisoformat(p["ts"]) - local_now), default=None)
        self.data["now_w"] = nearest["w"] if nearest else None


class SimulatedForecast:
    """Prognose ohne Internet für den Simulator (Sinuskurve mit wechselnder Bewölkung)."""

    def __init__(self, peak_w: float = 760.0, clock: Callable[[], datetime] | None = None):
        self.peak_w = peak_w
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.data: dict[str, Any] | None = None
        self.error: str | None = None
        self.loaded_at: datetime | None = None

    async def refresh(self, session: Any, now: datetime) -> None:
        self.recompute(now)

    def recompute(self, now: datetime) -> None:
        start = now.replace(hour=0, minute=30, second=0, microsecond=0, tzinfo=None)
        points, days = [], {}
        factors = [0.85, 0.55, 0.95]
        for i in range(72):
            t = start + timedelta(hours=i)
            h = t.hour + 0.5
            sun = math.sin(math.pi * (h - 7) / 10) if 7 <= h <= 17 else 0.0
            w = round(max(0.0, self.peak_w * sun * factors[i // 24]))
            points.append({"ts": t.isoformat(timespec="minutes"), "w": w})
            days[t.date()] = days.get(t.date(), 0) + w
        local_now = now.replace(tzinfo=None)
        today = local_now.date()
        icons = [("Überwiegend klar", "🌤️"), ("Bedeckt", "☁️"), ("Klar", "☀️")]
        self.data = {
            "points": points,
            "today_kwh": round(days.get(today, 0) / 1000, 2),
            "tomorrow_kwh": round(days.get(today + timedelta(days=1), 0) / 1000, 2),
            "rest_today_kwh": round(sum(
                p["w"] for p in points
                if datetime.fromisoformat(p["ts"]).date() == today and datetime.fromisoformat(p["ts"]) >= local_now
            ) / 1000, 2),
            "now_w": min(points, key=lambda p: abs(datetime.fromisoformat(p["ts"]) - local_now))["w"],
            "days": [
                {"date": (today + timedelta(days=i)).isoformat(), "kwh": round(days.get(today + timedelta(days=i), 0) / 1000, 2),
                 "t_max": 9 + i, "t_min": 2 + i, "icon": icons[i][1], "text": icons[i][0], "sun_h": [4.5, 1.0, 6.2][i]}
                for i in range(3)
            ],
            "weather": {"temp_c": 7.5, "text": icons[0][0], "icon": icons[0][1]},
        }
        self.loaded_at = now
