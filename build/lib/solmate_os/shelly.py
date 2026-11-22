"""Shelly-Strommessung über die lokale HTTP-Schnittstelle (ohne Cloud, ohne Schlüssel).

Unterstützt werden alle Generationen:

- Gen2/Gen3/Gen4 (Pro 3EM, EM Gen3, Pro EM, Plus Plug S, Plus PM, 1PM Mini …): ``GET /rpc/Shelly.GetStatus``
  - ``em:0.total_act_power``      Dreiphasen-Zähler (Pro 3EM, 3EM-63)
  - ``em1:N.act_power``           Einphasen-Zähler (Pro EM, EM Gen3)
  - ``switch:N.apower`` / ``pm1:N.apower``  Steckdosen und Schaltaktoren
- Gen1 (3EM, EM, Plug S, 1PM …): ``GET /status``
  - ``emeters[].power`` bzw. ``meters[].power``

Am Hauptzähler gilt: positiv = Netzbezug, negativ = Einspeisung (mit ``invert = true`` umkehrbar).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import aiohttp


@dataclass
class ShellyDevice:
    name: str
    host: str  # IP oder Hostname im Heimnetz, z. B. "192.168.1.40"
    role: str = "load"  # grid = Hauptzähler am Zählerkasten | load = einzelner Verbraucher
    password: str | None = None
    invert: bool = False


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def parse_gen2(status: dict[str, Any]) -> dict[str, Any]:
    em = status.get("em:0")
    if isinstance(em, dict) and _num(em.get("total_act_power")) is not None:
        phases = [_num(em.get(f"{p}_act_power")) for p in "abc"]
        return {"power_w": _num(em["total_act_power"]), "phases_w": [p for p in phases if p is not None]}
    for prefix, key in (("em1:", "act_power"), ("pm1:", "apower"), ("switch:", "apower")):
        values = [
            _num(v.get(key)) for k, v in status.items()
            if k.startswith(prefix) and isinstance(v, dict) and _num(v.get(key)) is not None
        ]
        if values:
            return {"power_w": sum(values), "phases_w": values if len(values) > 1 else []}
    raise ValueError("keine Leistungsmessung gefunden")


def parse_gen1(status: dict[str, Any]) -> dict[str, Any]:
    for key in ("emeters", "meters"):
        meters = [m for m in status.get(key) or [] if isinstance(m, dict) and _num(m.get("power")) is not None]
        if meters:
            values = [float(m["power"]) for m in meters]
            return {"power_w": sum(values), "phases_w": values if len(values) > 1 else []}
    if _num(status.get("total_power")) is not None:
        return {"power_w": float(status["total_power"]), "phases_w": []}
    raise ValueError("keine Leistungsmessung gefunden")


class ShellyClient:
    def __init__(self, device: ShellyDevice):
        self.device = device
        self.gen: int | None = None
        host = device.host if device.host.startswith("http") else f"http://{device.host}"
        self.base = host.rstrip("/")

    def _auth(self) -> dict[str, Any]:
        if not self.device.password:
            return {}
        if self.gen == 1:
            return {"auth": aiohttp.BasicAuth("admin", self.device.password)}
        digest = getattr(aiohttp, "DigestAuthMiddleware", None)  # Gen2+ verlangt Digest-Anmeldung
        if digest is None:
            raise RuntimeError("Passwortschutz bei Gen2-Shelly braucht aiohttp ≥ 3.12")
        return {"middlewares": (digest("admin", self.device.password),)}

    async def read(self, session: aiohttp.ClientSession) -> dict[str, Any]:
        if self.gen is None:
            async with session.get(f"{self.base}/shelly") as resp:
                resp.raise_for_status()
                info = await resp.json(content_type=None)
            self.gen = int(info.get("gen", 1) or 1)
        if self.gen >= 2:
            async with session.get(f"{self.base}/rpc/Shelly.GetStatus", **self._auth()) as resp:
                resp.raise_for_status()
                result = parse_gen2(await resp.json(content_type=None))
        else:
            async with session.get(f"{self.base}/status", **self._auth()) as resp:
                resp.raise_for_status()
                result = parse_gen1(await resp.json(content_type=None))
        if self.device.invert:
            result["power_w"] = -result["power_w"]
            result["phases_w"] = [-p for p in result["phases_w"]]
        if self.device.role == "load":
            result["power_w"] = abs(result["power_w"])
        result["power_w"] = round(result["power_w"], 1)
        result["phases_w"] = [round(p, 1) for p in result["phases_w"]]
        return result
