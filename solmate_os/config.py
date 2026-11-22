"""Konfiguration aus einer TOML-Datei (siehe ``config.example.toml``)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, time
from pathlib import Path
from typing import Any

from .forecast import ForecastConfig, Plane
from .tariff import Tariff
from .waste import DEFAULT_TYPES, WasteRule, WasteType


def parse_hhmm(value: str) -> time:
    hours, minutes = value.strip().split(":")
    if hours == "24" and minutes == "00":
        return time(23, 59, 59)
    return time(int(hours), int(minutes))


@dataclass
class ScheduleRule:
    """Zeitfenster mit Einspeisegrenzen. Fenster dürfen über Mitternacht gehen (z. B. 22:00–06:00)."""

    name: str
    start: time
    end: time
    min_w: float
    max_w: float
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)  # 0 = Montag

    def matches(self, weekday: int, now: time) -> bool:
        if self.start <= self.end:
            return weekday in self.weekdays and self.start <= now < self.end
        # über Mitternacht: der Wochentag gilt für den Startabend
        if now >= self.start:
            return weekday in self.weekdays
        return (weekday - 1) % 7 in self.weekdays and now < self.end


@dataclass
class HomeDevice:
    """Weiteres EcoFlow-Gerät für die Hausansicht (Stream-Einheit oder PowerStream)."""

    sn: str
    name: str = ""
    kind: str = "auto"  # auto | stream | powerstream
    watts_scale: float | None = None  # PowerStream liefert 0,1 W → 0.1 (automatisch)


@dataclass
class HomeConfig:
    title: str = "Unser Haus"
    interval_s: float = 60.0
    devices: list[HomeDevice] = field(default_factory=list)
    waste_types: list[WasteType] = field(default_factory=lambda: list(DEFAULT_TYPES))
    waste_ics_urls: list[str] = field(default_factory=list)
    waste_ics_files: list[Path] = field(default_factory=list)
    waste_rules: list[WasteRule] = field(default_factory=list)
    forecast: ForecastConfig | None = None
    tariff: Tariff = field(default_factory=Tariff)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HomeConfig":
        h = cls()
        h.title = data.get("title", h.title)
        h.interval_s = max(30.0, float(data.get("interval_s", h.interval_s)))
        for d in data.get("devices", []):
            kind = d.get("kind", "auto")
            if kind not in ("auto", "stream", "powerstream"):
                raise ValueError(f"Unbekannter Gerätetyp: {kind!r}")
            h.devices.append(
                HomeDevice(str(d["sn"]), d.get("name", ""), kind,
                           float(d["watts_scale"]) if "watts_scale" in d else None)
            )

        w = data.get("waste", {})
        names = {t.key: t for t in DEFAULT_TYPES}
        for key, t in (w.get("types") or {}).items():
            base = names.get(key)
            names[key] = WasteType(
                key,
                t.get("name", base.name if base else key),
                t.get("color", base.color if base else "#888888"),
                tuple(k.lower() for k in t.get("keywords", base.keywords if base else (key,))),
            )
        h.waste_types = list(names.values())
        urls = w.get("ics_url", [])
        h.waste_ics_urls = [urls] if isinstance(urls, str) else list(urls)
        files = w.get("ics_file", [])
        h.waste_ics_files = [Path(f).expanduser() for f in ([files] if isinstance(files, str) else files)]
        for r in w.get("rules", []):
            if r["type"] not in names:
                raise ValueError(f"Müll: unbekannte Tonne {r['type']!r}")
            moved = {
                date.fromisoformat(k): (_as_date(v) if v else None)
                for k, v in (r.get("moved") or {}).items()
            }
            h.waste_rules.append(
                WasteRule(r["type"], _as_date(r["start"]), int(r.get("every_weeks", 2)), moved)
            )

        f = data.get("forecast")
        if f:
            planes = [
                Plane(float(p["kwp"]), float(p.get("tilt", 30)), float(p.get("azimuth", 0)), p.get("name", ""))
                for p in f.get("planes", [])
            ]
            if not planes:
                planes = [Plane(float(f.get("kwp", 0.8)), float(f.get("tilt", 30)), float(f.get("azimuth", 0)))]
            h.forecast = ForecastConfig(
                float(f["latitude"]), float(f["longitude"]), planes,
                float(f.get("system_factor", 0.85)), float(f.get("max_ac_w", 800)),
            )

        t = data.get("tariff", {})
        h.tariff = Tariff(
            provider=t.get("provider", "EnBW"),
            name=t.get("name", ""),
            price_ct_kwh=float(t.get("price_ct_kwh", 0)),
            base_eur_month=float(t.get("base_eur_month", 0)),
            deposit_eur_month=float(t.get("deposit_eur_month", 0)),
            contract_start=_as_date(t["contract_start"]) if t.get("contract_start") else None,
            meter_start_kwh=float(t["meter_start_kwh"]) if "meter_start_kwh" in t else None,
            feed_in_ct_kwh=float(t.get("feed_in_ct_kwh", 0)),
            app_url=t.get("app_url", Tariff.app_url),
            customer_number=str(t.get("customer_number", "")),
        )
        return h


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


@dataclass
class Config:
    mode: str = "simulator"  # simulator | cloud | local
    serial: str = "SIM-0001"
    password: str | None = None
    uri: str | None = None

    sim_speed: float = 1.0
    sim_capacity_wh: float = 1600.0
    sim_pv_peak_w: float = 880.0

    control_enabled: bool = True
    interval_s: float = 30.0
    legal_limit_w: float = 800.0
    reserve_percent: float = 15.0
    default_min_w: float = 0.0
    default_max_w: float = 800.0
    max_temp_c: float = 60.0
    derate_w: float = 300.0
    schedule: list[ScheduleRule] = field(default_factory=list)

    boost_watts: float = 800.0
    boost_minutes: float = 20.0

    ecoflow_mode: str = "off"  # off | simulator | cloud
    ecoflow_host: str = "api-e.ecoflow.com"
    ecoflow_access_key: str | None = None
    ecoflow_secret_key: str | None = None
    ecoflow_stream_sn: str = ""
    ecoflow_meter_sn: str | None = None
    ecoflow_grid_import_positive: bool = True
    ecoflow_strategy: str = "balance"  # balance | solmate_first | stream_first | monitor
    household_limit_w: float = 800.0
    coord_margin_w: float = 20.0
    coord_deadband_w: float = 25.0

    host: str = "127.0.0.1"
    pin: str | None = None
    port: int = 8080
    db_path: Path = Path("solmate.sqlite3")
    retention_days: int = 365
    token_path: Path = Path.home() / ".config" / "solmate-os" / "tokens.json"
    home: HomeConfig = field(default_factory=HomeConfig)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        dev = data.get("device", {})
        sim = data.get("simulator", {})
        ctl = data.get("control", {})
        web = data.get("web", {})
        sto = data.get("storage", {})
        cfg = cls()
        cfg.mode = dev.get("mode", cfg.mode)
        if cfg.mode not in ("simulator", "cloud", "local"):
            raise ValueError(f"Unbekannter Modus: {cfg.mode!r}")
        cfg.serial = str(dev.get("serial", cfg.serial))
        cfg.password = os.environ.get("SOLMATE_PASSWORD") or dev.get("password") or None
        cfg.uri = dev.get("uri") or None

        cfg.sim_speed = float(sim.get("speed", cfg.sim_speed))
        cfg.sim_capacity_wh = float(sim.get("capacity_wh", cfg.sim_capacity_wh))
        cfg.sim_pv_peak_w = float(sim.get("pv_peak_w", cfg.sim_pv_peak_w))

        cfg.control_enabled = bool(ctl.get("enabled", cfg.control_enabled))
        cfg.interval_s = max(5.0, float(ctl.get("interval_s", cfg.interval_s)))
        cfg.legal_limit_w = float(ctl.get("legal_limit_w", cfg.legal_limit_w))
        cfg.reserve_percent = float(ctl.get("reserve_percent", cfg.reserve_percent))
        cfg.default_min_w = float(ctl.get("default_min_w", cfg.default_min_w))
        cfg.default_max_w = float(ctl.get("default_max_w", cfg.legal_limit_w))
        cfg.max_temp_c = float(ctl.get("max_temp_c", cfg.max_temp_c))
        cfg.derate_w = float(ctl.get("derate_w", cfg.derate_w))
        cfg.schedule = [
            ScheduleRule(
                name=r.get("name", f"Regel {i + 1}"),
                start=parse_hhmm(r["start"]),
                end=parse_hhmm(r["end"]),
                min_w=float(r.get("min_w", 0)),
                max_w=float(r.get("max_w", cfg.legal_limit_w)),
                weekdays=tuple(r.get("weekdays", range(7))),
            )
            for i, r in enumerate(ctl.get("schedule", []))
        ]

        boost = data.get("boost", {})
        cfg.boost_watts = float(boost.get("watts", cfg.boost_watts))
        cfg.boost_minutes = float(boost.get("minutes", cfg.boost_minutes))

        eco = data.get("ecoflow", {})
        cfg.ecoflow_mode = eco.get("mode", cfg.ecoflow_mode)
        if cfg.ecoflow_mode not in ("off", "simulator", "cloud"):
            raise ValueError(f"Unbekannter EcoFlow-Modus: {cfg.ecoflow_mode!r}")
        cfg.ecoflow_host = eco.get("host", cfg.ecoflow_host)
        cfg.ecoflow_access_key = os.environ.get("ECOFLOW_ACCESS_KEY") or eco.get("access_key")
        cfg.ecoflow_secret_key = os.environ.get("ECOFLOW_SECRET_KEY") or eco.get("secret_key")
        cfg.ecoflow_stream_sn = str(eco.get("stream_sn", ""))
        cfg.ecoflow_meter_sn = eco.get("meter_sn") or None
        cfg.ecoflow_grid_import_positive = bool(eco.get("grid_import_positive", True))
        cfg.ecoflow_strategy = eco.get("strategy", cfg.ecoflow_strategy)
        if cfg.ecoflow_strategy not in ("balance", "solmate_first", "stream_first", "monitor"):
            raise ValueError(f"Unbekannte Strategie: {cfg.ecoflow_strategy!r}")
        cfg.household_limit_w = float(eco.get("household_limit_w", cfg.legal_limit_w))
        cfg.coord_margin_w = float(eco.get("margin_w", cfg.coord_margin_w))
        cfg.coord_deadband_w = float(eco.get("deadband_w", cfg.coord_deadband_w))
        if cfg.ecoflow_mode == "cloud" and not (
            cfg.ecoflow_access_key and cfg.ecoflow_secret_key and cfg.ecoflow_stream_sn
        ):
            raise ValueError("EcoFlow: access_key, secret_key und stream_sn sind nötig.")

        cfg.host = web.get("host", cfg.host)
        cfg.pin = os.environ.get("SOLMATE_OS_PIN") or (str(web["pin"]) if web.get("pin") else None)
        cfg.port = int(web.get("port", cfg.port))
        cfg.db_path = Path(sto.get("path", cfg.db_path))
        cfg.retention_days = int(sto.get("retention_days", cfg.retention_days))
        cfg.home = HomeConfig.from_dict(data.get("home", {}))
        if cfg.home.devices and cfg.mode != "simulator" and not (cfg.ecoflow_access_key and cfg.ecoflow_secret_key):
            raise ValueError("Hausansicht: für [[home.devices]] werden die EcoFlow-Schlüssel aus [ecoflow] benötigt.")
        if "token_path" in dev:
            cfg.token_path = Path(dev["token_path"]).expanduser()
        return cfg

    @classmethod
    def load(cls, path: Path | None) -> "Config":
        if path is None:
            return cls()
        with open(path, "rb") as fh:
            return cls.from_dict(tomllib.load(fh))
