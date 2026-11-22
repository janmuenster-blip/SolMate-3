"""Regelautomatik: liest zyklisch Messwerte, speichert sie und setzt die Einspeisegrenzen.

Mit EcoFlow-STREAM-Anbindung wird der SolMate zusätzlich auf den Smart Meter abgestimmt:
beide Speicher decken gemeinsam den Hausverbrauch, ohne ins Netz zu schieben, und zusammen
wird die Haushaltsgrenze (z. B. 800 W) nie überschritten.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .backend import SolMateBackend
from .config import Config
from .ecoflow import StreamState
from .models import InjectionSettings, LiveValues
from .storage import Storage

log = logging.getLogger(__name__)


@dataclass
class Decision:
    min_w: float
    max_w: float
    reason: str


def headroom(cfg: Config, stream: StreamState | None) -> float:
    """Wie viel der SolMate höchstens einspeisen darf, damit Stream + SolMate die Haushaltsgrenze einhalten."""
    limit = cfg.legal_limit_w
    if stream is not None and stream.feed_in_w is not None:
        limit = min(limit, cfg.household_limit_w - stream.feed_in_w)
    return max(0.0, limit)


def decide(cfg: Config, now: datetime, live: LiveValues | None) -> Decision:
    """Zeitplan + Schutzfunktionen, ohne EcoFlow."""
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


def coordinate(
    cfg: Config, base: Decision, live: LiveValues, stream: StreamState, solmate_leads: bool
) -> Decision:
    """Abstimmung mit dem Stream über den Smart Meter (``grid_w`` > 0 = Netzbezug).

    Führt der SolMate, übernimmt er den Bedarf, den gerade Stream und Netz decken; der Stream regelt
    über seinen Smart Meter von selbst zurück. Führt der Stream, springt der SolMate nur bei Netzbezug ein.
    Ergebnis ist ein fester Sollwert (min = max), begrenzt durch Zeitplan-Maximum und Haushaltsgrenze.
    """
    cap = min(base.max_w, headroom(cfg, stream))
    grid = stream.grid_w or 0.0
    current = live.inject_w
    if solmate_leads:
        # Den ganzen Bedarf übernehmen, den gerade Stream und Netz decken; der Stream regelt dann zurück.
        target = current + (stream.feed_in_w or 0.0) + grid - cfg.coord_margin_w
        who = "SolMate führt"
    elif grid > cfg.coord_deadband_w:
        target = current + grid - cfg.coord_margin_w  # Stream schafft es nicht allein
        who = "Stream führt, SolMate hilft"
    else:
        target = current - 50  # langsam zurücknehmen, solange der Stream genug liefert
        who = "Stream führt"
    target = max(0.0, min(target, cap))
    return Decision(round(target), round(target), f"{base.reason}, {who} (Netz {grid:+.0f} W)")


class Controller:
    def __init__(
        self,
        cfg: Config,
        backend: SolMateBackend,
        storage: Storage,
        clock: Callable[[], datetime] | None = None,
        stream: Any | None = None,
    ):
        self.cfg = cfg
        self.backend = backend
        self.storage = storage
        self.stream_backend = stream
        self.clock = clock or getattr(backend, "now", None) or (lambda: datetime.now().astimezone())
        self.automatic = cfg.control_enabled
        self.override_until: datetime | None = None
        self.online = False
        self.last_live: LiveValues | None = None
        self.last_stream: StreamState | None = None
        self.stream_error: str | None = None
        self.last_decision: Decision | None = None
        self.applied: InjectionSettings | None = None
        self.last_error: str | None = None
        self.solmate_leads = True
        self.boost_until: datetime | None = None
        self.boost_requested_w = 0.0
        self.boost_total_s = 0
        self.boost_sent_w = 0.0
        self.boost_note: str | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

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

    async def _read_stream(self) -> None:
        if self.stream_backend is None:
            return
        try:
            self.last_stream = await self.stream_backend.get_state()
            self.storage.add_stream_sample(self.last_stream)
            if self.stream_error:
                self.event("info", "EcoFlow Stream wieder erreichbar.")
            self.stream_error = None
        except Exception as exc:  # Stream-Ausfall darf den SolMate-Betrieb nicht stören
            msg = f"EcoFlow nicht erreichbar: {exc}"
            if msg != self.stream_error:
                self.event("warn", msg)
            self.stream_error = msg
            self.last_stream = None

    def _stream_fresh(self) -> StreamState | None:
        s = self.last_stream
        if s is None or self.stream_error:
            return None
        return s

    async def tick(self) -> None:
        async with self._lock:
            live = await self.backend.get_live_values()
            if not self.online:
                self.event("info", "SolMate ist online.")
            self.online = True
            self.last_error = None
            self.last_live = live
            self.storage.add_sample(live)
            await self._read_stream()

            if self.applied is None:
                self.applied = await self.backend.get_injection_settings()

            now = self.clock()
            if await self._boost_tick(now):
                return
            if self.override_until and now >= self.override_until:
                self.override_until = None
                self.event("info", "Manuelle Übersteuerung beendet, Automatik übernimmt wieder.")
            if not self.automatic or self.override_until:
                return

            decision = decide(self.cfg, now, live)
            stream = self._stream_fresh()
            if stream is not None:
                if self.cfg.ecoflow_strategy != "monitor" and stream.grid_w is not None:
                    self._update_leader(live, stream)
                    decision = coordinate(self.cfg, decision, live, stream, self.solmate_leads)
                    if (
                        self.applied
                        and self.applied.min_w == self.applied.max_w
                        and abs(decision.max_w - self.applied.max_w) < self.cfg.coord_deadband_w
                    ):
                        self.last_decision = decision
                        return  # kleine Änderungen nicht senden, schont das Gerät
                else:
                    cap = round(headroom(self.cfg, stream))
                    if decision.max_w > cap:
                        decision = Decision(min(decision.min_w, cap), cap, decision.reason + ", Haushaltsgrenze")
            self.last_decision = decision
            await self._apply(decision.min_w, decision.max_w, self.cfg.reserve_percent, decision.reason)

    def _update_leader(self, live: LiveValues, stream: StreamState) -> None:
        strategy = self.cfg.ecoflow_strategy
        if strategy == "solmate_first":
            self.solmate_leads = True
        elif strategy == "stream_first":
            self.solmate_leads = False
        elif stream.soc_percent is not None:
            # Batterieausgleich: der vollere Speicher führt; 5 % Hysterese gegen Hin- und Herspringen.
            diff = live.soc_percent - stream.soc_percent
            if self.solmate_leads and diff < -5:
                self.solmate_leads = False
            elif not self.solmate_leads and diff > 5:
                self.solmate_leads = True

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

    # --- Boost ------------------------------------------------------------------------------------

    def _boost_allowed(self) -> tuple[float, str | None]:
        stream = self._stream_fresh()
        cap = headroom(self.cfg, stream)
        watts = min(self.boost_requested_w, cap)
        if watts < self.boost_requested_w - 1 and stream is not None:
            return watts, f"Stream speist {stream.feed_in_w:.0f} W ein – SolMate auf {watts:.0f} W begrenzt"
        return watts, None

    async def _boost_tick(self, now: datetime) -> bool:
        """Hält eine laufende Boost-Sitzung aktuell. Gibt True zurück, solange sie läuft."""
        if self.boost_until is None:
            return False
        if now >= self.boost_until:
            self.boost_until = None
            self.boost_note = None
            self.event("info", "Boost beendet, Automatik übernimmt wieder.")
            return False
        watts, note = self._boost_allowed()
        self.boost_note = note
        if abs(watts - self.boost_sent_w) >= self.cfg.coord_deadband_w:
            remaining = int((self.boost_until - now).total_seconds())
            await self.backend.set_boost(remaining, watts)
            self.boost_sent_w = watts
        return True

    async def start_boost(self, minutes: float, watts: float) -> None:
        async with self._lock:
            if minutes <= 0 or watts <= 0:
                await self._stop_boost()
                return
            self.boost_requested_w = min(watts, self.cfg.legal_limit_w)
            self.boost_until = self.clock() + timedelta(minutes=minutes)
            self.boost_total_s = int(minutes * 60)
            allowed, self.boost_note = self._boost_allowed()
            await self.backend.set_boost(int(minutes * 60), allowed)
            self.boost_sent_w = allowed
            self.event("info", f"Boost: {allowed:.0f} W für {minutes:.0f} min" + (f" ({self.boost_note})" if self.boost_note else ""))
        self.poke()

    async def _stop_boost(self) -> None:
        await self.backend.set_boost(0, 0)
        if self.boost_until is not None:
            self.event("info", "Boost manuell beendet.")
        self.boost_until = None
        self.boost_note = None
        self.boost_sent_w = 0.0

    async def boost(self, seconds: int, watts: float) -> None:
        """Kompatibel zur Kommandozeile/REST: 0 Sekunden beendet den Boost."""
        await self.start_boost(seconds / 60, watts)

    # --- manuelle Bedienung -----------------------------------------------------------------------

    async def manual(self, min_w: float, max_w: float, reserve: float, minutes: int) -> None:
        async with self._lock:
            max_w = max(0.0, min(max_w, headroom(self.cfg, self._stream_fresh())))
            min_w = max(0.0, min(min_w, max_w))
            reserve = max(0.0, min(reserve, 100.0))
            if self.applied is None:
                self.applied = await self.backend.get_injection_settings()
            await self._apply(round(min_w), round(max_w), round(reserve), "manuell")
            self.override_until = self.clock() + timedelta(minutes=minutes) if minutes > 0 else None

    def set_automatic(self, enabled: bool) -> None:
        self.automatic = enabled
        self.override_until = None
        self.event("info", "Automatik eingeschaltet." if enabled else "Automatik ausgeschaltet.")
        self.poke()

    def status(self) -> dict[str, Any]:
        now = self.clock()
        boost = None
        if self.boost_until and now < self.boost_until:
            boost = {
                "until": self.boost_until.isoformat(),
                "remaining_s": int((self.boost_until - now).total_seconds()),
                "total_s": self.boost_total_s,
                "watts": self.boost_sent_w,
                "requested_w": self.boost_requested_w,
                "note": self.boost_note,
            }
        stream = None
        if self.cfg.ecoflow_mode != "off":
            stream = {
                "mode": self.cfg.ecoflow_mode,
                "strategy": self.cfg.ecoflow_strategy,
                "leader": "solmate" if self.solmate_leads else "stream",
                "state": self.last_stream.to_dict() if self.last_stream else None,
                "error": self.stream_error,
                "household_limit_w": self.cfg.household_limit_w,
            }
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
            "boost": boost,
            "boost_preset": {"watts": self.cfg.boost_watts, "minutes": self.cfg.boost_minutes},
            "stream": stream,
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
