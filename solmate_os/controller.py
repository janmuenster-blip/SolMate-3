"""Regelautomatik: liest zyklisch Messwerte, speichert sie und setzt die Einspeisegrenzen."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .backend import SolMateBackend
from .config import Config
from .models import InjectionSettings, LiveValues
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class Decision:
    min_w: float
    max_w: float
    reason: str


def decide(cfg: Config, now: datetime, live: LiveValues | None) -> Decision:
    """Reine Funktion: berechnet die gewünschten Einspeisegrenzen für den Zeitpunkt ``now``."""
    rule = next((r for r in cfg.schedule if r.matches(now.weekday(), now.time())), None)
    if rule:
        min_w, max_w, reason = rule.min_w, rule.max_w, f"Zeitplan „{rule.name}“"
    else:
        min_w, max_w, reason = cfg.default_min_w, cfg.default_max_w, "Standard"

    if live and live.temperature_c is not None and live.temperature_c >= cfg.max_temp_c:
        max_w = min(max_w, cfg.derate_w)
        reason += f", Temperaturschutz ({live.temperature_c:.0f} °C)"

    # Gesetzliche Grenze (z. B. 800 W für Balkonkraftwerke in DE/AT) nie überschreiten.
    max_w = max(0.0, min(max_w, cfg.legal_limit_w))
    min_w = max(0.0, min(min_w, max_w))
    return Decision(round(min_w), round(max_w), reason)


class Controller:
    def __init__(
        self,
        cfg: Config,
        backend: SolMateBackend,
        storage: Storage,
        clock: Callable[[], datetime] | None = None,
    ):
        self.cfg = cfg
        self.backend = backend
        self.storage = storage
        self.clock = clock or getattr(backend, "now", None) or (lambda: datetime.now().astimezone())
        self.automatic = cfg.control_enabled
        self.override_until: datetime | None = None
        self.online = False
        self.last_live: LiveValues | None = None
        self.last_decision: Decision | None = None
        self.applied: InjectionSettings | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()

    # --- Lebenszyklus -----------------------------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="solmate-controller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def event(self, level: str, message: str) -> None:
        getattr(log, "warning" if level == "warn" else level, log.info)(message)
        self.storage.add_event(level, message)

    async def _run(self) -> None:
        backoff = self.cfg.interval_s
        last_prune = datetime.min.replace(tzinfo=timezone.utc)
        while True:
            try:
                await self.tick()
                backoff = self.cfg.interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # Gerät offline, Netz weg …
                self.online = False
                msg = f"Fehler bei der Kommunikation: {exc}"
                if msg != self.last_error:
                    self.event("error", msg)
                self.last_error = msg
                backoff = min(backoff * 2, 600)
            now = datetime.now(timezone.utc)
            if now - last_prune > timedelta(days=1):
                self.storage.prune(self.cfg.retention_days)
                last_prune = now
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), backoff)
            except asyncio.TimeoutError:
                pass

    def poke(self) -> None:
        """Nächsten Zyklus sofort ausführen (z. B. nach Änderung in der Weboberfläche)."""
        self._wake.set()

    # --- ein Regelzyklus --------------------------------------------------------------------------

    async def tick(self) -> None:
        live = await self.backend.get_live_values()
        if not self.online:
            self.event("info", "SolMate ist online.")
        self.online = True
        self.last_error = None
        self.last_live = live
        self.storage.add_sample(live)

        if self.applied is None:
            self.applied = await self.backend.get_injection_settings()

        now = self.clock()
        if self.override_until and now >= self.override_until:
            self.override_until = None
            self.event("info", "Manuelle Übersteuerung beendet, Automatik übernimmt wieder.")
        if not self.automatic or self.override_until:
            return

        decision = decide(self.cfg, now, live)
        self.last_decision = decision
        await self._apply(decision.min_w, decision.max_w, self.cfg.reserve_percent, decision.reason)

    async def _apply(self, min_w: float, max_w: float, reserve: float, reason: str) -> None:
        cur = self.applied
        changes = []
        # Reihenfolge so wählen, dass min <= max auch zwischendurch gilt.
        if cur is None or max_w != cur.max_w:
            if cur is None or max_w > cur.max_w:
                await self.backend.set_max_injection(max_w)
                changes.append(f"max {max_w:.0f} W")
        if cur is None or min_w != cur.min_w:
            await self.backend.set_min_injection(min_w)
            changes.append(f"min {min_w:.0f} W")
        if cur is not None and max_w < cur.max_w:
            await self.backend.set_max_injection(max_w)
            changes.append(f"max {max_w:.0f} W")
        if cur is None or reserve != cur.min_battery_percent:
            await self.backend.set_min_battery_percent(reserve)
            changes.append(f"Reserve {reserve:.0f} %")
        self.applied = InjectionSettings(min_w, max_w, reserve)
        if changes:
            self.event("info", f"Einstellungen gesetzt ({reason}): " + ", ".join(changes))

    # --- manuelle Bedienung -----------------------------------------------------------------------

    async def manual(self, min_w: float, max_w: float, reserve: float, minutes: int) -> None:
        max_w = max(0.0, min(max_w, self.cfg.legal_limit_w))
        min_w = max(0.0, min(min_w, max_w))
        reserve = max(0.0, min(reserve, 100.0))
        if self.applied is None:
            self.applied = await self.backend.get_injection_settings()
        await self._apply(round(min_w), round(max_w), round(reserve), "manuell")
        self.override_until = self.clock() + timedelta(minutes=minutes) if minutes > 0 else None

    async def boost(self, seconds: int, watts: float) -> None:
        watts = max(0.0, min(watts, self.cfg.legal_limit_w))
        await self.backend.set_boost(seconds, watts)
        self.event("info", f"Boost: {watts:.0f} W für {seconds // 60} min" if seconds else "Boost beendet")

    def set_automatic(self, enabled: bool) -> None:
        self.automatic = enabled
        self.override_until = None
        self.event("info", "Automatik eingeschaltet." if enabled else "Automatik ausgeschaltet.")
        self.poke()

    def status(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "serial": self.backend.serial,
            "mode": self.cfg.mode,
            "online": self.online,
            "device_time": now.isoformat(),
            "automatic": self.automatic,
            "override_until": self.override_until.isoformat() if self.override_until else None,
            "live": self.last_live.to_dict() if self.last_live else None,
            "applied": self.applied.to_dict() if self.applied else None,
            "decision": self.last_decision.__dict__ if self.last_decision else None,
            "error": self.last_error,
            "legal_limit_w": self.cfg.legal_limit_w,
            "schedule": [
                {
                    "name": r.name,
                    "start": r.start.strftime("%H:%M"),
                    "end": r.end.strftime("%H:%M"),
                    "min_w": r.min_w,
                    "max_w": r.max_w,
                    "weekdays": list(r.weekdays),
                }
                for r in self.cfg.schedule
            ],
        }
