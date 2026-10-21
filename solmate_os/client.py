"""WebSocket-Client für die SolMate-API von EET.

Implementiert dasselbe Protokoll wie das offizielle ``solmate-sdk`` (MIT, github.com/eet-energy/solmate-sdk):
Jede Anfrage ist ein JSON-Objekt ``{"route", "id", "data"}``, die Antwort enthält ``data`` oder ``error``.

Ablauf: verbinden → ``login`` (liefert Token) → ``authenticate`` (ggf. Weiterleitung an anderen Server)
→ geschützte Routen wie ``live_values``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import aiohttp

from .models import InjectionSettings, LiveValues

log = logging.getLogger(__name__)

CLOUD_URI = "wss://sol.eet.energy:9124"
LOCAL_URI = "ws://sun2plug.local:9124/"
CLOUD_DEVICE_ID = "solmate-os"
LOCAL_DEVICE_ID = "local_webinterface"


class SolMateError(Exception):
    """Fehler, den das Gerät bzw. der EET-Server gemeldet hat."""


def password_hash(password: str) -> str:
    """Stufe-1-Hash des Benutzerpassworts, genau wie im offiziellen SDK (inkl. Zeilenumbruch)."""
    return base64.encodebytes(hashlib.sha256(password.encode()).digest()).decode()


class TokenStore:
    """Speichert Anmelde-Token je Seriennummer, damit das Passwort nicht erneut nötig ist."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def put(self, key: str, token: str) -> None:
        data = self._load()
        data[key] = token
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data), encoding="utf-8")
        self.path.chmod(0o600)

    def delete(self, key: str) -> None:
        data = self._load()
        if data.pop(key, None) is not None:
            self.path.write_text(json.dumps(data), encoding="utf-8")


class SolMateClient:
    """Asynchroner Client für einen SolMate (Cloud oder lokal)."""

    def __init__(
        self,
        serial: str,
        password: str | None = None,
        *,
        local: bool = False,
        uri: str | None = None,
        token_store: TokenStore | None = None,
        timeout: float = 30.0,
    ):
        self.serial = serial
        self._password = password
        self.local = local
        self.uri = uri or (LOCAL_URI if local else CLOUD_URI)
        self.device_id = LOCAL_DEVICE_ID if local else CLOUD_DEVICE_ID
        self.token_store = token_store
        self.timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._authenticated = False

    # --- Verbindung -------------------------------------------------------------------------------

    @property
    def _token_key(self) -> str:
        return f"{'local' if self.local else 'cloud'}:{self.serial}"

    async def start(self) -> None:
        await self._connect_and_auth()

    async def stop(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()
        self._ws = self._session = None
        self._authenticated = False

    async def _open(self, uri: str) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._session is None:
            self._session = aiohttp.ClientSession()
        log.debug("Verbinde mit %s", uri)
        self._ws = await self._session.ws_connect(uri, heartbeat=30)

    async def _connect_and_auth(self) -> None:
        self._authenticated = False
        await self._open(self.uri)
        token = self.token_store.get(self._token_key) if self.token_store else None
        if token is None:
            token = await self.login()
        try:
            await self._authenticate(token, follow_redirect=not self.local)
        except SolMateError:
            # Token abgelaufen oder ungültig: einmal neu anmelden.
            if self.token_store:
                self.token_store.delete(self._token_key)
            if not self._password:
                raise
            await self._open(self.uri)
            await self._authenticate(await self.login(), follow_redirect=not self.local)
        self._authenticated = True
        log.info("Mit SolMate %s verbunden (%s)", self.serial, self.uri)

    async def login(self) -> str:
        if not self._password:
            raise SolMateError("Kein Passwort angegeben und kein gespeichertes Token vorhanden.")
        data = await self._raw_request(
            "login",
            {
                "serial_num": self.serial,
                "user_password_hash": password_hash(self._password),
                "device_id": self.device_id,
            },
        )
        if not data.get("success"):
            raise SolMateError("Anmeldung fehlgeschlagen – Seriennummer oder Passwort falsch?")
        token = data["signature"]
        if self.token_store:
            self.token_store.put(self._token_key, token)
        return token

    async def _authenticate(self, token: str, follow_redirect: bool) -> None:
        payload = {"serial_num": self.serial, "signature": token, "device_id": self.device_id}
        data = await self._raw_request("authenticate", payload)
        redirect = data.get("redirect") if isinstance(data, dict) else None
        if follow_redirect and redirect and redirect != self.uri:
            # Der Lastverteiler von EET nennt den Server, an dem das Gerät hängt.
            log.info("Weiterleitung an %s", redirect)
            self.uri = redirect
            await self._open(self.uri)
            await self._raw_request("authenticate", payload)

    async def _raw_request(self, route: str, data: dict[str, Any]) -> Any:
        if self._ws is None or self._ws.closed:
            raise ConnectionError("WebSocket ist nicht verbunden.")
        self._next_id += 1
        await self._ws.send_str(json.dumps({"route": route, "id": self._next_id, "data": data}))
        msg = await asyncio.wait_for(self._ws.receive(), self.timeout)
        if msg.type != aiohttp.WSMsgType.TEXT:
            raise ConnectionError(f"Verbindung unterbrochen ({msg.type.name}).")
        response = json.loads(msg.data)
        if "error" in response:
            raise SolMateError(str(response["error"]))
        return response.get("data")

    async def request(self, route: str, data: dict[str, Any] | None = None) -> Any:
        """Sendet eine Anfrage; baut die Verbindung bei Abbruch einmal neu auf."""
        async with self._lock:
            for attempt in (1, 2):
                try:
                    if not self._authenticated or self._ws is None or self._ws.closed:
                        await self._connect_and_auth()
                    return await self._raw_request(route, data or {})
                except (ConnectionError, aiohttp.ClientError, asyncio.TimeoutError):
                    self._authenticated = False
                    if attempt == 2:
                        raise
                    log.warning("Verbindung verloren, neuer Versuch …")
                    await asyncio.sleep(1)

    # --- Routen -----------------------------------------------------------------------------------

    async def is_online(self) -> bool:
        if self.local:
            return self._ws is not None and not self._ws.closed
        data = await self.request("check_online", {"serial_num": self.serial})
        return bool(data.get("online"))

    async def get_info(self) -> dict[str, Any]:
        return await self.request("get_solmate_info")

    async def get_live_values(self) -> LiveValues:
        return LiveValues.from_api(await self.request("live_values"))

    async def get_injection_settings(self) -> InjectionSettings:
        raw = await self.request("get_injection_settings") or {}

        def pick(*keys: str) -> float:
            for k in keys:
                if isinstance(raw.get(k), (int, float)):
                    return float(raw[k])
            return 0.0

        return InjectionSettings(
            min_w=pick("user_minimum_injection", "minimum_injection"),
            max_w=pick("user_maximum_injection", "maximum_injection"),
            min_battery_percent=pick("user_minimum_battery_percentage", "minimum_battery_percentage"),
        )

    async def set_max_injection(self, watts: float) -> None:
        await self.request("set_user_maximum_injection", {"injection": int(watts)})

    async def set_min_injection(self, watts: float) -> None:
        await self.request("set_user_minimum_injection", {"injection": int(watts)})

    async def set_min_battery_percent(self, percent: float) -> None:
        await self.request("set_user_minimum_battery_percentage", {"battery_percentage": int(percent)})

    async def set_boost(self, seconds: int, watts: float) -> None:
        await self.request("set_boost_injection", {"time": int(seconds), "wattage": int(watts)})
