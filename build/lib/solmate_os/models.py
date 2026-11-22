"""Datenmodelle für Messwerte und Einstellungen des SolMate."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _num(raw: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    """Liest den ersten vorhandenen numerischen Wert aus mehreren möglichen Schlüsseln."""
    for key in keys:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return default


@dataclass
class LiveValues:
    """Normalisierte Live-Werte.

    Vorzeichen von ``battery_w``: positiv = Batterie wird geladen, negativ = entladen.
    """

    timestamp: datetime
    pv_w: float
    inject_w: float
    battery_w: float
    soc_percent: float
    temperature_c: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, raw: dict[str, Any], now: datetime | None = None) -> "LiveValues":
        """Wandelt die Antwort der Route ``live_values`` um.

        Die SolMate-API liefert ``battery_state`` als Anteil 0..1; ältere Firmware teils in Prozent.
        """
        soc = _num(raw, "battery_state", "soc")
        if soc <= 1.0:
            soc *= 100.0
        temp = raw.get("temperature")
        return cls(
            timestamp=now or datetime.now(timezone.utc),
            pv_w=_num(raw, "pv_power"),
            inject_w=_num(raw, "inject_power"),
            battery_w=_num(raw, "battery_flow"),
            soc_percent=round(max(0.0, min(100.0, soc)), 1),
            temperature_c=float(temp) if isinstance(temp, (int, float)) else None,
            raw=dict(raw),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data


@dataclass
class InjectionSettings:
    """Einspeisegrenzen in Watt und Batteriereserve in Prozent."""

    min_w: float
    max_w: float
    min_battery_percent: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)
