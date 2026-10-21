"""Konfiguration aus einer TOML-Datei (siehe ``config.example.toml``)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any


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

    host: str = "127.0.0.1"
    port: int = 8080
    db_path: Path = Path("solmate.sqlite3")
    retention_days: int = 365
    token_path: Path = Path.home() / ".config" / "solmate-os" / "tokens.json"

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

        cfg.host = web.get("host", cfg.host)
        cfg.port = int(web.get("port", cfg.port))
        cfg.db_path = Path(sto.get("path", cfg.db_path))
        cfg.retention_days = int(sto.get("retention_days", cfg.retention_days))
        if "token_path" in dev:
            cfg.token_path = Path(dev["token_path"]).expanduser()
        return cfg

    @classmethod
    def load(cls, path: Path | None) -> "Config":
        if path is None:
            return cls()
        with open(path, "rb") as fh:
            return cls.from_dict(tomllib.load(fh))
