"""Stromtarif (z. B. EnBW) und Kosten.

EnBW bietet für Privatkunden keine öffentliche Schnittstelle an; die Tarifdaten (Arbeitspreis,
Grundpreis, Abschlag) stehen in der EnBW-App bzw. unter „Mein EnBW“ und werden einmal in die
``config.toml`` übernommen. Zählerstände trägt man in der Hausansicht ein (wie in der EnBW-App).

Daraus wird berechnet:
- Kosten heute/Monat aus dem Netzbezug, den der EcoFlow Smart Meter misst
- Ersparnis durch den eigenen Solarstrom (SolMate, Stream, PowerStream)
- Hochrechnung des Jahresverbrauchs aus den Zählerständen → voraussichtliche Nachzahlung oder Guthaben
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


@dataclass
class Tariff:
    provider: str = "EnBW"
    name: str = ""
    price_ct_kwh: float = 0.0  # Arbeitspreis brutto
    base_eur_month: float = 0.0  # Grundpreis brutto
    deposit_eur_month: float = 0.0  # monatlicher Abschlag
    contract_start: date | None = None  # Beginn des Abrechnungsjahres
    meter_start_kwh: float | None = None  # Zählerstand zu Beginn des Abrechnungsjahres
    feed_in_ct_kwh: float = 0.0  # Einspeisevergütung (Balkonkraftwerk meist 0)
    app_url: str = "https://www.enbw.com/mein-enbw"
    customer_number: str = ""

    @property
    def configured(self) -> bool:
        return self.price_ct_kwh > 0


def _eur(ct: float) -> float:
    return round(ct / 100, 2)


def period_costs(t: Tariff, import_wh: float | None, own_wh: float, export_wh: float, days: float) -> dict[str, Any]:
    """Kosten und Ersparnis für einen Zeitraum (``days`` für den anteiligen Grundpreis)."""
    base = t.base_eur_month * 12 / 365 * days
    out: dict[str, Any] = {
        "import_kwh": round(import_wh / 1000, 2) if import_wh is not None else None,
        "own_kwh": round(own_wh / 1000, 2),
        "export_kwh": round(export_wh / 1000, 2),
        "base_eur": round(base, 2),
    }
    out["energy_eur"] = _eur(import_wh / 1000 * t.price_ct_kwh) if import_wh is not None else None
    out["total_eur"] = round(out["energy_eur"] + base, 2) if out["energy_eur"] is not None else None
    used_own = max(0.0, own_wh - export_wh)
    out["saved_eur"] = _eur(used_own / 1000 * t.price_ct_kwh + export_wh / 1000 * t.feed_in_ct_kwh)
    return out


def year_projection(t: Tariff, readings: list[dict[str, Any]], today: date) -> dict[str, Any] | None:
    """Hochrechnung aus Zählerständen. Braucht Startstand + einen Stand oder zwei Stände."""
    points = [(date.fromisoformat(r["day"]), float(r["kwh"])) for r in readings]
    if t.contract_start and t.meter_start_kwh is not None:
        points.append((t.contract_start, t.meter_start_kwh))
    points = sorted(set(points))
    if t.contract_start:
        points = [p for p in points if p[0] >= t.contract_start]
    if len(points) < 2:
        return None
    (d0, k0), (d1, k1) = points[0], points[-1]
    days = (d1 - d0).days
    if days < 7 or k1 < k0:
        return None
    per_day = (k1 - k0) / days
    year_kwh = per_day * 365
    cost = year_kwh * t.price_ct_kwh / 100 + t.base_eur_month * 12
    deposits = t.deposit_eur_month * 12
    start = t.contract_start or d0
    return {
        "since": d0.isoformat(),
        "last_reading": {"day": d1.isoformat(), "kwh": k1},
        "used_kwh": round(k1 - k0, 1),
        "per_day_kwh": round(per_day, 2),
        "year_kwh": round(year_kwh),
        "year_cost_eur": round(cost, 2),
        "deposits_eur": round(deposits, 2),
        # positiv = Guthaben, negativ = Nachzahlung
        "balance_eur": round(deposits - cost, 2) if deposits else None,
        "fair_deposit_eur": round(cost / 12, 2),
        "days_into_year": (today - start).days,
        "reading_age_days": (today - d1).days,
    }


def summary(
    t: Tariff,
    readings: list[dict[str, Any]],
    today_energy: dict[str, float | None],
    month_energy: dict[str, float | None],
    today: date,
    day_fraction: float,
) -> dict[str, Any]:
    return {
        "configured": t.configured,
        "provider": t.provider,
        "name": t.name,
        "price_ct_kwh": t.price_ct_kwh,
        "base_eur_month": t.base_eur_month,
        "deposit_eur_month": t.deposit_eur_month,
        "customer_number": t.customer_number,
        "app_url": t.app_url,
        "today": period_costs(
            t, today_energy.get("import_wh"), today_energy.get("own_wh") or 0.0,
            today_energy.get("export_wh") or 0.0, day_fraction,
        ),
        "month": period_costs(
            t, month_energy.get("import_wh"), month_energy.get("own_wh") or 0.0,
            month_energy.get("export_wh") or 0.0, today.day - 1 + day_fraction,
        ),
        "projection": year_projection(t, readings, today),
        "readings": readings[-12:],
    }
