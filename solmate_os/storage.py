"""Speichert Messwerte in SQLite und liefert Verlauf sowie Tagesbilanzen."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .ecoflow import StreamState
from .models import LiveValues

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts TEXT NOT NULL,
    pv_w REAL, inject_w REAL, battery_w REAL, soc REAL, temp_c REAL
);
CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS stream_samples (
    ts TEXT NOT NULL,
    pv_w REAL, battery_w REAL, soc REAL, feed_in_w REAL, grid_w REAL, load_w REAL
);
CREATE INDEX IF NOT EXISTS stream_samples_ts ON stream_samples(ts);
CREATE TABLE IF NOT EXISTS events (
    ts TEXT NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add_sample(self, v: LiveValues) -> None:
        self.db.execute(
            "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)",
            (
                v.timestamp.astimezone(timezone.utc).isoformat(),
                v.pv_w, v.inject_w, v.battery_w, v.soc_percent, v.temperature_c,
            ),
        )
        self.db.commit()

    def add_stream_sample(self, s: StreamState) -> None:
        self.db.execute(
            "INSERT INTO stream_samples VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                s.timestamp.astimezone(timezone.utc).isoformat(),
                s.pv_w, s.battery_w, s.soc_percent, s.feed_in_w, s.grid_w, s.load_w,
            ),
        )
        self.db.commit()

    def stream_history(self, since: datetime, max_points: int = 500) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM stream_samples WHERE ts >= ? ORDER BY ts",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
        step = max(1, len(rows) // max_points)
        return [dict(r) for r in rows[::step]]

    def grid_energy(self, since: datetime) -> dict[str, float]:
        """Netzbezug und -einspeisung aus den Smart-Meter-Werten (Wh)."""
        rows = self.db.execute(
            "SELECT ts, grid_w FROM stream_samples WHERE ts >= ? AND grid_w IS NOT NULL ORDER BY ts",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
        out = {"grid_import_wh": 0.0, "grid_export_wh": 0.0}
        for a, b in zip(rows, rows[1:]):
            dt_h = (datetime.fromisoformat(b["ts"]) - datetime.fromisoformat(a["ts"])).total_seconds() / 3600
            if dt_h <= 0 or dt_h > 0.5:
                continue
            e = (a["grid_w"] + b["grid_w"]) / 2 * dt_h
            out["grid_import_wh" if e > 0 else "grid_export_wh"] += abs(e)
        return {k: round(v, 1) for k, v in out.items()}

    def add_event(self, level: str, message: str, ts: datetime | None = None) -> None:
        ts = (ts or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.db.execute("INSERT INTO events VALUES (?, ?, ?)", (ts.isoformat(), level, message))
        self.db.commit()

    def events(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def history(self, since: datetime, max_points: int = 500) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM samples WHERE ts >= ? ORDER BY ts",
            (since.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
        step = max(1, len(rows) // max_points)
        return [dict(r) for r in rows[::step]]

    def energy(self, since: datetime, until: datetime | None = None) -> dict[str, float]:
        """Integriert Leistung über die Zeit (Trapezregel) zu Energie in Wh."""
        query = "SELECT ts, pv_w, inject_w, battery_w FROM samples WHERE ts >= ?"
        params = [since.astimezone(timezone.utc).isoformat()]
        if until is not None:
            query += " AND ts < ?"
            params.append(until.astimezone(timezone.utc).isoformat())
        rows = self.db.execute(query + " ORDER BY ts", params).fetchall()
        out = {"pv_wh": 0.0, "inject_wh": 0.0, "charge_wh": 0.0, "discharge_wh": 0.0}
        for a, b in zip(rows, rows[1:]):
            dt_h = (datetime.fromisoformat(b["ts"]) - datetime.fromisoformat(a["ts"])).total_seconds() / 3600
            if dt_h <= 0 or dt_h > 0.5:  # Lücken (Gerät offline) nicht überbrücken
                continue
            out["pv_wh"] += (a["pv_w"] + b["pv_w"]) / 2 * dt_h
            out["inject_wh"] += (a["inject_w"] + b["inject_w"]) / 2 * dt_h
            bat = (a["battery_w"] + b["battery_w"]) / 2 * dt_h
            out["charge_wh" if bat > 0 else "discharge_wh"] += abs(bat)
        return {k: round(v, 1) for k, v in out.items()}

    def prune(self, retention_days: int) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
        cur = self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM stream_samples WHERE ts < ?", (cutoff,))
        self.db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount
