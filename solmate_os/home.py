"""Hausansicht: fasst Müllabfuhr, Akkus, Solarertrag mit Prognose und Stromkosten zusammen.

Läuft als eigene Hintergrundaufgabe neben der SolMate-Regelung und stört diese nicht:
fällt eine Quelle aus (Internet, EcoFlow-API), zeigt die Hausansicht die letzten bekannten Werte.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from datetime import datetime, timedelta
from typing import Any, Callable

import aiohttp

from .config import Config, HomeDevice
from .ecoflow import EcoFlowClient, _find
from .forecast import SimulatedForecast, SolarForecast
from .storage import Storage
from .tariff import summary as tariff_summary
from .waste import WasteCalendar

log = logging.getLogger(__name__)

POWERSTREAM_KEYS = ("pv1InputWatts", "pv2InputWatts", "invOutputWatts")


def detect_kind(raw: dict[str, Any]) -> str:
    if any(_find(raw, k) is not None for k in POWERSTREAM_KEYS):
        return "powerstream"
    return "stream"


def parse_device(dev: HomeDevice, raw: dict[str, Any]) -> dict[str, Any]:
    """Liest Ladestand, PV-Leistung und Abgabe aus den Rohdaten eines EcoFlow-Geräts.

    PowerStream (Feldnamen laut EcoFlow-Developer-Doku, Werte in 0,1 W):
    ``20_1.pv1InputWatts``, ``20_1.pv2InputWatts``, ``20_1.invOutputWatts``, ``20_1.batSoc``, ``20_1.batInputWatts``.
    """
    kind = dev.kind if dev.kind != "auto" else detect_kind(raw)
    if kind == "powerstream":
        scale = dev.watts_scale if dev.watts_scale is not None else 0.1
        pv1, pv2 = _find(raw, "pv1InputWatts"), _find(raw, "pv2InputWatts")
        out = _find(raw, "invOutputWatts")
        bat = _find(raw, "batInputWatts")
        soc = _find(raw, "batSoc")
        pv = None if pv1 is None and pv2 is None else ((pv1 or 0) + (pv2 or 0)) * scale
        return {
            "kind": kind,
            "pv_w": round(pv, 1) if pv is not None else None,
            "pv1_w": round(pv1 * scale, 1) if pv1 is not None else None,
            "pv2_w": round(pv2 * scale, 1) if pv2 is not None else None,
            "out_w": round(max(0.0, out * scale), 1) if out is not None else None,
            "battery_w": round(bat * scale, 1) if bat is not None else None,
            # Ohne angeschlossenen Akku meldet der PowerStream 0 %.
            "soc_percent": soc if soc else None,
        }
    scale = dev.watts_scale if dev.watts_scale is not None else 1.0
    conn = _find(raw, "gridConnectionPower")
    pv = _find(raw, "powGetPvSum")
    bat = _find(raw, "powGetBpCms")
    return {
        "kind": "stream",
        "pv_w": pv * scale if pv is not None else None,
        "out_w": max(0.0, -conn * scale) if conn is not None else None,
        "battery_w": bat * scale if bat is not None else None,
        "soc_percent": _find(raw, "cmsBattSoc") or _find(raw, "f32ShowSoc") or _find(raw, "soc"),
    }


class SimulatedDevices:
    """Weitere EcoFlow-Geräte für den Simulator: ein Stream AC Pro und ein PowerStream."""

    def __init__(self, forecast: SimulatedForecast, seed: int | None = None):
        self.forecast = forecast
        self.rng = random.Random(seed)
        self.soc = 72.0
        self.devices = [
            HomeDevice("SIM-STREAM-AC", "Stream AC Pro", "stream"),
            HomeDevice("SIM-POWERSTREAM", "PowerStream", "powerstream"),
        ]

    def read(self, sn: str, now: datetime) -> dict[str, Any]:
        h = now.hour + now.minute / 60
        sun = math.sin(math.pi * (h - 7) / 10) if 7 <= h <= 17 else 0.0
        if sn == "SIM-POWERSTREAM":
            pv = max(0.0, 760 * sun * self.rng.uniform(0.6, 1.0))
            return {"20_1.pv1InputWatts": round(pv * 5.2), "20_1.pv2InputWatts": round(pv * 4.8),
                    "20_1.invOutputWatts": round(min(800, pv) * 9.5), "20_1.batSoc": 0}
        self.soc = max(8.0, min(100.0, self.soc + (0.4 if sun > 0.3 else -0.2)))
        return {"cmsBattSoc": round(self.soc), "powGetPvSum": 0, "gridConnectionPower": -round(120 * (1 - sun)),
                "powGetBpCms": round(80 * sun - 120 * (1 - sun))}


class HomeService:
    def __init__(
        self,
        cfg: Config,
        storage: Storage,
        ctl: Any = None,
        ecoflow: EcoFlowClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.cfg = cfg
        self.home = cfg.home
        self.storage = storage
        self.ctl = ctl
        # Für Leistungswerte die Gerätezeit verwenden (Simulator kann schneller laufen),
        # für Kalender und Kosten immer die echte Uhrzeit.
        self.power_clock = clock or (ctl.clock if ctl is not None else lambda: datetime.now().astimezone())
        self.wall_clock: Callable[[], datetime] = lambda: datetime.now().astimezone()
        self.waste = WasteCalendar(
            self.home.waste_types, self.home.waste_ics_urls, self.home.waste_ics_files, self.home.waste_rules
        )
        self.simulated = cfg.mode == "simulator"
        self.forecast: Any = None
        if self.home.forecast and self.home.forecast.planes:
            self.forecast = SolarForecast(self.home.forecast)
        elif self.simulated:
            self.forecast = SimulatedForecast(clock=self.power_clock)
        self.sim_devices = SimulatedDevices(self.forecast) if self.simulated and not self.home.devices else None
        self.devices = list(self.sim_devices.devices) if self.sim_devices else list(self.home.devices)
        if cfg.ecoflow_mode != "off":  # der Haupt-Stream kommt schon über die Regelung
            self.devices = [d for d in self.devices if d.sn != cfg.ecoflow_stream_sn]
        self.ecoflow = ecoflow
        self._own_ecoflow = False
        if self.ecoflow is None and self.devices and not self.sim_devices:
            if isinstance(getattr(ctl, "stream_backend", None), EcoFlowClient):
                self.ecoflow = ctl.stream_backend
            elif cfg.ecoflow_access_key and cfg.ecoflow_secret_key:
                self.ecoflow = EcoFlowClient(cfg.ecoflow_access_key, cfg.ecoflow_secret_key, "", host=cfg.ecoflow_host)
                self._own_ecoflow = True
        self.device_state: dict[str, dict[str, Any]] = {}
        self.device_error: dict[str, str] = {}
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._energy_cache: tuple[datetime, dict[str, Any]] | None = None

    # --- Lebenszyklus -----------------------------------------------------------------------------

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=20), headers={"User-Agent": "solmate-os"}, trust_env=True
        )
        self._task = asyncio.create_task(self._run(), name="home-service")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._session:
            await self._session.close()
        if self._own_ecoflow and self.ecoflow:
            await self.ecoflow.stop()

    async def _run(self) -> None:
        last_waste = last_forecast = datetime.min
        while True:
            now = datetime.now()
            try:
                if now - last_waste > timedelta(hours=6) or (self.waste.error and now - last_waste > timedelta(minutes=30)):
                    await self.waste.refresh(self._session, self.wall_clock().date())
                    last_waste = now
                if self.forecast and now - last_forecast > timedelta(minutes=30):
                    await self.forecast.refresh(self._session, self.power_clock())
                    last_forecast = now
                await self.poll_devices()
            except asyncio.CancelledError:
                raise
            except Exception:  # nie die ganze Hausansicht anhalten
                log.exception("Hausansicht: Fehler beim Aktualisieren")
            await asyncio.sleep(self.home.interval_s)

    async def poll_devices(self) -> None:
        now = self.power_clock()
        for dev in self.devices:
            try:
                if self.sim_devices:
                    raw = self.sim_devices.read(dev.sn, now)
                elif self.ecoflow:
                    raw = await self.ecoflow.quota_all(dev.sn)
                else:
                    raise RuntimeError("EcoFlow-Schlüssel fehlen")
                state = parse_device(dev, raw)
                state["updated"] = now.isoformat()
                self.device_state[dev.sn] = state
                self.device_error.pop(dev.sn, None)
                self.storage.add_home_sample(
                    now, dev.sn, state.get("soc_percent"), state.get("pv_w"), state.get("out_w"), state.get("battery_w")
                )
            except Exception as exc:
                self.device_error[dev.sn] = f"{dev.name or dev.sn}: {exc}"

    # --- Zusammenfassung --------------------------------------------------------------------------

    def _energy(self) -> dict[str, Any]:
        """Eigene Erzeugung und Netzbezug für heute und den laufenden Monat (5 min zwischengespeichert)."""
        now = self.wall_clock()
        if self._energy_cache and now - self._energy_cache[0] < timedelta(minutes=5):
            return self._energy_cache[1]
        pnow = self.power_clock()
        midnight = pnow.replace(hour=0, minute=0, second=0, microsecond=0)
        month = midnight.replace(day=1)
        has_meter = self.ctl is not None and self.ctl.stream_backend is not None
        out = {}
        for key, since in (("today", midnight), ("month", month)):
            own = self.storage.energy(since)["inject_wh"] + self.storage.stream_feed_energy(since)
            per_device = {}
            for dev in self.devices:
                e = self.storage.home_energy(dev.sn, since)
                per_device[dev.sn] = e
                own += e["out_wh"]
            grid = self.storage.grid_energy(since) if has_meter else {}
            out[key] = {
                "own_wh": own,
                "import_wh": grid.get("grid_import_wh"),
                "export_wh": grid.get("grid_export_wh", 0.0),
                "devices": per_device,
            }
        self._energy_cache = (now, out)
        return out

    def _batteries(self) -> list[dict[str, Any]]:
        out = []
        ctl = self.ctl
        if ctl is not None and ctl.last_live is not None:
            l = ctl.last_live
            out.append({
                "name": "SolMate 3", "kind": "solmate", "soc_percent": l.soc_percent, "pv_w": l.pv_w,
                "out_w": l.inject_w, "battery_w": l.battery_w, "online": ctl.online,
            })
        if ctl is not None and ctl.cfg.ecoflow_mode != "off":
            s = ctl.last_stream
            out.append({
                "name": "Stream", "kind": "stream", "online": s is not None and not ctl.stream_error,
                "soc_percent": s.soc_percent if s else None, "pv_w": s.pv_w if s else None,
                "out_w": s.feed_in_w if s else None, "battery_w": s.battery_w if s else None,
                "error": ctl.stream_error,
            })
        for dev in self.devices:
            st = self.device_state.get(dev.sn, {})
            if st.get("kind", dev.kind) == "powerstream" and st.get("soc_percent") is None:
                continue  # PowerStream ohne Akku → nur in der Solar-Kachel
            out.append({
                "name": dev.name or dev.sn, "kind": st.get("kind", dev.kind), "online": dev.sn not in self.device_error,
                "soc_percent": st.get("soc_percent"), "pv_w": st.get("pv_w"), "out_w": st.get("out_w"),
                "battery_w": st.get("battery_w"), "error": self.device_error.get(dev.sn),
            })
        return out

    def _solar(self, energy: dict[str, Any]) -> dict[str, Any]:
        pnow = self.power_clock()
        powerstreams = []
        for dev in self.devices:
            st = self.device_state.get(dev.sn)
            kind = st.get("kind") if st else dev.kind
            if kind != "powerstream":
                continue
            powerstreams.append({
                "sn": dev.sn, "name": dev.name or "PowerStream", "pv_w": st.get("pv_w") if st else None,
                "pv1_w": st.get("pv1_w") if st else None, "pv2_w": st.get("pv2_w") if st else None,
                "out_w": st.get("out_w") if st else None,
                "today_kwh": round(energy["today"]["devices"].get(dev.sn, {}).get("pv_wh", 0) / 1000, 2),
                "error": self.device_error.get(dev.sn),
            })
        since = pnow.replace(hour=0, minute=0, second=0, microsecond=0)
        actual = []
        if powerstreams:
            actual = self.storage.home_history(powerstreams[0]["sn"], since, 200)
            actual = [{"ts": r["ts"], "w": r["pv_w"]} for r in actual if r["pv_w"] is not None]
        if self.forecast:
            self.forecast.recompute(pnow)
        return {
            "powerstreams": powerstreams,
            "actual": actual,
            "forecast": self.forecast.data if self.forecast else None,
            "forecast_error": self.forecast.error if self.forecast else None,
            "forecast_configured": self.forecast is not None,
        }

    def summary(self) -> dict[str, Any]:
        wall = self.wall_clock()
        pnow = self.power_clock()
        energy = self._energy()
        day_fraction = (pnow.hour * 3600 + pnow.minute * 60 + pnow.second) / 86400
        return {
            "title": self.home.title,
            "now": wall.isoformat(),
            "device_time": pnow.isoformat(),
            "simulated": self.simulated,
            "waste": self.waste.summary(wall.date()),
            "batteries": self._batteries(),
            "solar": self._solar(energy),
            "tariff": tariff_summary(
                self.home.tariff, self.storage.meter_readings(), energy["today"], energy["month"],
                pnow.date(), day_fraction,
            ),
            "has_smart_meter": self.ctl is not None and self.ctl.stream_backend is not None,
            "house": self._house(),
        }

    def _house(self) -> dict[str, Any]:
        """Momentaufnahme fürs ganze Haus: Solar aller Geräte, Verbrauch und Netz (Smart Meter)."""
        ctl = self.ctl
        solar = 0.0
        if ctl is not None and ctl.last_live is not None:
            solar += ctl.last_live.pv_w
        stream = ctl.last_stream if ctl is not None and not ctl.stream_error else None
        if stream is not None and stream.pv_w is not None:
            solar += stream.pv_w
        for dev in self.devices:
            solar += self.device_state.get(dev.sn, {}).get("pv_w") or 0.0
        return {
            "solar_w": round(solar),
            "load_w": stream.load_w if stream else None,
            "grid_w": stream.grid_w if stream else None,
        }
