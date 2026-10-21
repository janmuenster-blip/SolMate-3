"""Anbindung an das EcoFlow-STREAM-System (Stream Ultra/Pro/AC, Smart Meter).

Nutzt die offizielle EcoFlow IoT-Open-API (developer-eu.ecoflow.com): REST mit HMAC-SHA256-Signatur.
Feldnamen laut Community-Dokumentation (github.com/htims1989/ecoflow-public-api, docs/entity_mapping.md):

- ``powGetPvSum``          PV-Leistung gesamt (W)
- ``powGetBpCms``          Batterieleistung (W, Entladen negativ)
- ``cmsBattSoc``           Ladestand (%)
- ``powGetSysGrid``        Netzleistung am Smart Meter (W, Vorzeichen konfigurierbar)
- ``powGetSysLoad``        Hausverbrauch (W)
- ``gridConnectionPower``  Wechselrichter am Netz (W, Einspeisung negativ)
- ``backupReverseSoc``     Reserve-Ladestand (%)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import aiohttp

log = logging.getLogger(__name__)

EU_HOST = "api-e.ecoflow.com"
GLOBAL_HOST = "api.ecoflow.com"


class EcoFlowError(Exception):
    pass


@dataclass
class StreamState:
    """Normalisierter Zustand des Stream-Systems. ``grid_w`` positiv = Netzbezug."""

    timestamp: datetime
    pv_w: float | None = None
    battery_w: float | None = None
    soc_percent: float | None = None
    feed_in_w: float | None = None  # was der Stream gerade ins Hausnetz abgibt
    grid_w: float | None = None
    load_w: float | None = None
    reserve_percent: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        d.pop("raw")
        return d


def _flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    items: dict[str, Any] = {}
    for key, value in data.items():
        new_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            items.update(_flatten(value, new_key))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    items.update(_flatten(item, f"{new_key}[{i}]"))
                else:
                    items[f"{new_key}[{i}]"] = item
        else:
            items[new_key] = value
    return items


def _sign_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def sign(secret_key: str, access_key: str, params: dict[str, Any], nonce: str, timestamp: str) -> str:
    """HMAC-SHA256 über sortierte, flachgeklopfte Parameter + accessKey/nonce/timestamp."""
    flat = _flatten(params)
    base = f"accessKey={access_key}&nonce={nonce}&timestamp={timestamp}"
    query = "&".join(f"{k}={_sign_value(flat[k])}" for k in sorted(flat))
    target = f"{query}&{base}" if query else base
    return hmac.new(secret_key.encode(), target.encode(), hashlib.sha256).hexdigest()


def _find(raw: dict[str, Any], key: str) -> float | None:
    """Sucht ein Feld, auch wenn die API es mit Präfix liefert (z. B. ``20_1.powGetSysGrid``)."""
    if isinstance(raw.get(key), (int, float)) and not isinstance(raw.get(key), bool):
        return float(raw[key])
    for k, v in raw.items():
        if k.endswith("." + key) and isinstance(v, (int, float)) and not isinstance(v, bool):
            return float(v)
    return None


def parse_state(stream: dict[str, Any], meter: dict[str, Any] | None, grid_import_positive: bool) -> StreamState:
    grid = _find(meter, "powGetSysGrid") if meter else None
    if grid is None:
        grid = _find(stream, "powGetSysGrid")
    if grid is not None and not grid_import_positive:
        grid = -grid
    conn = _find(stream, "gridConnectionPower")
    return StreamState(
        timestamp=datetime.now(timezone.utc),
        pv_w=_find(stream, "powGetPvSum"),
        battery_w=_find(stream, "powGetBpCms"),
        soc_percent=_find(stream, "cmsBattSoc") or _find(stream, "f32ShowSoc"),
        feed_in_w=max(0.0, -conn) if conn is not None else None,
        grid_w=grid,
        load_w=_find(stream, "powGetSysLoad"),
        reserve_percent=_find(stream, "backupReverseSoc"),
        raw={**stream, **({"meter": meter} if meter else {})},
    )


class EcoFlowClient:
    """REST-Client für die EcoFlow-Developer-API."""

    def __init__(
        self,
        access_key: str,
        secret_key: str,
        stream_sn: str,
        meter_sn: str | None = None,
        host: str = EU_HOST,
        grid_import_positive: bool = True,
    ):
        self.access_key = access_key
        self.secret_key = secret_key
        self.stream_sn = stream_sn
        self.meter_sn = meter_sn
        self.host = host
        self.grid_import_positive = grid_import_positive
        self._session: aiohttp.ClientSession | None = None
        self._main_sn: str | None = None

    async def start(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None

    async def _call(self, method: str, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if self._session is None:
            await self.start()
        params = params or {}
        nonce = str(random.randint(100000, 999999))
        ts = str(int(time.time() * 1000))
        headers = {
            "accessKey": self.access_key,
            "nonce": nonce,
            "timestamp": ts,
            "sign": sign(self.secret_key, self.access_key, params, nonce, ts),
        }
        base = self.host if self.host.startswith("http") else f"https://{self.host}"
        url = f"{base}/iot-open/sign{endpoint}"
        kwargs: dict[str, Any] = {"headers": headers}
        if method == "GET":
            flat = _flatten(params)
            kwargs["params"] = {k: _sign_value(flat[k]) for k in sorted(flat)}
        else:
            kwargs["json"] = params
        async with self._session.request(method, url, **kwargs) as resp:  # type: ignore[union-attr]
            resp.raise_for_status()
            payload = await resp.json(content_type=None)
        code = str(payload.get("code", "0"))
        if code not in ("0", ""):
            raise EcoFlowError(f"EcoFlow-API {code}: {payload.get('message', 'Fehler')}")
        return payload.get("data")

    async def device_list(self) -> list[dict[str, Any]]:
        return await self._call("GET", "/device/list") or []

    async def quota_all(self, sn: str) -> dict[str, Any]:
        return await self._call("GET", "/device/quota/all", {"sn": sn}) or {}

    async def get_state(self) -> StreamState:
        stream = await self.quota_all(self.stream_sn)
        meter = await self.quota_all(self.meter_sn) if self.meter_sn else None
        return parse_state(stream, meter, self.grid_import_positive)

    async def _main(self) -> str:
        if self._main_sn is None:
            data = await self._call("GET", "/device/system/main/sn", {"sn": self.stream_sn}) or {}
            self._main_sn = data.get("sn") or self.stream_sn
        return self._main_sn

    async def set_params(self, params: dict[str, Any]) -> None:
        body = {
            "sn": await self._main(),
            "cmdId": 17, "cmdFunc": 254, "dirDest": 1, "dirSrc": 1, "dest": 2, "needAck": True,
            "params": params,
        }
        await self._call("PUT", "/device/quota", body)

    async def set_reserve(self, percent: int) -> None:
        await self.set_params({"cfgBackupReverseSoc": max(3, min(95, int(percent)))})


class SimulatedStream:
    """Simulierter Stream Ultra mit Smart Meter und Hausverbrauch.

    Regelt wie das Original auf Nulleinspeisung: deckt den Rest des Hausverbrauchs, den der SolMate nicht deckt.
    """

    def __init__(
        self,
        solmate: Any,
        *,
        capacity_wh: float = 1920.0,
        pv_peak_w: float = 1200.0,
        max_out_w: float = 800.0,
        start_soc: float = 60.0,
        reserve: float = 10.0,
        seed: int | None = None,
        load_fn: Callable[[datetime], float] | None = None,
    ):
        self.solmate = solmate
        self.capacity_wh = capacity_wh
        self.pv_peak_w = pv_peak_w
        self.max_out_w = max_out_w
        self.soc = start_soc
        self.reserve = reserve
        self._rng = random.Random(seed)
        self._load_fn = load_fn or self._default_load
        self._last_t: datetime | None = None
        self.feed_in = 0.0

    def _default_load(self, t: datetime) -> float:
        h = t.hour + t.minute / 60
        base = 180.0
        morning = 450 * math.exp(-((h - 7.5) ** 2) / 0.8)
        noon = 350 * math.exp(-((h - 12.5) ** 2) / 0.6)
        evening = 650 * math.exp(-((h - 19) ** 2) / 2.5)
        return base + morning + noon + evening + self._rng.uniform(-30, 30)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def get_state(self) -> StreamState:
        t = self.solmate.now()
        dt_h = 0.0 if self._last_t is None else max(0.0, (t - self._last_t).total_seconds() / 3600)
        self._last_t = t
        h = t.hour + t.minute / 60
        pv = max(0.0, self.pv_peak_w * math.sin(math.pi * (h - 6.5) / 13)) if 6.5 <= h <= 19.5 else 0.0
        load = self._load_fn(t)
        sol = self.solmate._last.inject_w
        need = max(0.0, load - sol)  # Rest, den der Stream decken soll
        can_discharge = self.soc > self.reserve
        out = min(need, self.max_out_w, pv + (self.max_out_w if can_discharge else 0.0))
        battery = pv - out
        if battery > 0 and self.soc >= 100:
            battery = 0.0
        self.soc = min(100.0, max(0.0, self.soc + battery * dt_h / self.capacity_wh * 100))
        self.feed_in = out
        grid = load - sol - out
        raw = {
            "powGetPvSum": round(pv, 1), "powGetBpCms": round(battery, 1), "cmsBattSoc": round(self.soc),
            "powGetSysGrid": round(grid, 1), "powGetSysLoad": round(load, 1),
            "gridConnectionPower": round(-out, 1), "backupReverseSoc": self.reserve,
        }
        state = parse_state(raw, None, True)
        state.timestamp = t.astimezone(timezone.utc)
        return state

    async def set_reserve(self, percent: int) -> None:
        self.reserve = float(percent)
