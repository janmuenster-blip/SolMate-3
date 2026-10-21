"""Simulierter SolMate zum Ausprobieren und Testen ohne echtes Gerät."""

from __future__ import annotations

import math
import random
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .models import InjectionSettings, LiveValues


class SimulatedSolMate:
    """Einfaches Energiemodell: PV-Ertrag über den Tag, Batterie, Wechselrichter mit Einspeisegrenze.

    ``speed`` beschleunigt die Zeit (z. B. 60 = eine Minute pro Sekunde).
    """

    def __init__(
        self,
        serial: str = "SIM-0001",
        *,
        capacity_wh: float = 1600.0,
        pv_peak_w: float = 880.0,
        inverter_limit_w: float = 800.0,
        start_soc: float = 50.0,
        speed: float = 1.0,
        start_time: datetime | None = None,
        seed: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.serial = serial
        self.capacity_wh = capacity_wh
        self.pv_peak_w = pv_peak_w
        self.inverter_limit_w = inverter_limit_w
        self.speed = speed
        self.soc = start_soc
        self.settings = InjectionSettings(min_w=0.0, max_w=inverter_limit_w, min_battery_percent=10.0)
        self._clock = clock
        self._t0_real = clock()
        self._t0_sim = start_time or datetime.now().astimezone()
        self._last_real = self._t0_real
        self._rng = random.Random(seed)
        self._cloud = 1.0
        self._boost_until: datetime | None = None
        self._boost_w = 0.0
        self._last = LiveValues(self.now(), 0, 0, 0, self.soc)

    # --- Zeit & Physik ----------------------------------------------------------------------------

    def now(self) -> datetime:
        return self._t0_sim + timedelta(seconds=(self._clock() - self._t0_real) * self.speed)

    def _pv_power(self, t: datetime) -> float:
        hour = t.hour + t.minute / 60 + t.second / 3600
        sun = math.sin(math.pi * (hour - 6) / 14) if 6 <= hour <= 20 else 0.0
        # Wolken ändern sich langsam (Zufallsspaziergang zwischen 0.3 und 1.0).
        self._cloud = min(1.0, max(0.3, self._cloud + self._rng.uniform(-0.05, 0.05)))
        return max(0.0, self.pv_peak_w * sun * self._cloud)

    def step(self) -> LiveValues:
        real = self._clock()
        dt_h = (real - self._last_real) * self.speed / 3600
        self._last_real = real
        t = self.now()
        s = self.settings
        pv = self._pv_power(t)

        boost = self._boost_until is not None and t < self._boost_until
        if boost:
            target = self._boost_w
        else:
            # PV bis zur Obergrenze einspeisen, mindestens aber die Untergrenze aus der Batterie.
            target = min(max(pv, s.min_w), s.max_w)
        target = min(target, self.inverter_limit_w)

        battery = pv - target
        if battery < 0 and self.soc <= s.min_battery_percent and not boost:
            battery, target = 0.0, pv  # Reserve schützen
        if battery < 0 and self.soc <= 0:
            battery, target = 0.0, pv
        if battery > 0 and self.soc >= 100:
            extra = min(battery, self.inverter_limit_w - target)
            target += extra
            battery = 0.0  # Rest wird abgeregelt

        self.soc = min(100.0, max(0.0, self.soc + battery * dt_h / self.capacity_wh * 100))
        temp = 22 + pv / 60 + self._rng.uniform(-0.5, 0.5)
        raw = {
            "pv_power": round(pv, 1),
            "inject_power": round(target, 1),
            "battery_flow": round(battery, 1),
            "battery_state": round(self.soc / 100, 4),
            "temperature": round(temp, 1),
        }
        self._last = LiveValues.from_api(raw, now=t.astimezone(timezone.utc))
        return self._last

    # --- Backend-Schnittstelle --------------------------------------------------------------------

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def is_online(self) -> bool:
        return True

    async def get_info(self) -> dict[str, Any]:
        return {"serial": self.serial, "version": "simulator", "capacity_wh": self.capacity_wh}

    async def get_live_values(self) -> LiveValues:
        return self.step()

    async def get_injection_settings(self) -> InjectionSettings:
        return InjectionSettings(**self.settings.to_dict())

    async def set_max_injection(self, watts: float) -> None:
        self.settings.max_w = max(0.0, min(float(watts), self.inverter_limit_w))

    async def set_min_injection(self, watts: float) -> None:
        self.settings.min_w = max(0.0, min(float(watts), self.inverter_limit_w))

    async def set_min_battery_percent(self, percent: float) -> None:
        self.settings.min_battery_percent = max(0.0, min(float(percent), 100.0))

    async def set_boost(self, seconds: int, watts: float) -> None:
        if seconds <= 0:
            self._boost_until = None
            return
        self._boost_until = self.now() + timedelta(seconds=seconds)
        self._boost_w = float(watts)
