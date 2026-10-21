"""Prüft den WebSocket-Client gegen einen nachgebauten EET-Server."""

import json

from aiohttp import web
from aiohttp.test_utils import TestServer

from solmate_os.client import SolMateClient, SolMateError, TokenStore, password_hash

import pytest

SERIAL = "S123"
PASSWORD = "geheim"


class FakeSol:
    def __init__(self, redirect_to=None):
        self.redirect_to = redirect_to
        self.calls = []
        self.settings = {"user_minimum_injection": 50, "user_maximum_injection": 600,
                         "user_minimum_battery_percentage": 20}

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        authed = False
        async for msg in ws:
            req = json.loads(msg.data)
            route, data = req["route"], req["data"]
            self.calls.append((request.path, route, data))
            reply = {"id": req["id"]}
            if route == "login":
                ok = data["user_password_hash"] == password_hash(PASSWORD) and data["serial_num"] == SERIAL
                reply["data"] = {"success": ok, "signature": "TOKEN" if ok else None}
            elif route == "authenticate":
                if data["signature"] != "TOKEN":
                    reply["error"] = "bad signature"
                else:
                    authed = True
                    redirect = self.redirect_to if request.path == "/" else None
                    reply["data"] = {"redirect": redirect}
            elif not authed:
                reply["error"] = "not authenticated"
            elif route == "live_values":
                reply["data"] = {"pv_power": 420.5, "inject_power": 300, "battery_flow": 120.5,
                                 "battery_state": 0.63, "temperature": 31.2}
            elif route == "get_injection_settings":
                reply["data"] = self.settings
            elif route == "set_user_maximum_injection":
                self.settings["user_maximum_injection"] = data["injection"]
                reply["data"] = {}
            elif route == "check_online":
                reply["data"] = {"online": True}
            else:
                reply["data"] = {}
            await ws.send_str(json.dumps(reply))
        return ws


async def start(fake):
    app = web.Application()
    app.router.add_get("/", fake.handler)
    app.router.add_get("/node2", fake.handler)
    server = TestServer(app)
    await server.start_server()
    return server


def uri(server, path="/"):
    return f"ws://{server.host}:{server.port}{path}"


async def test_login_live_values_and_settings(tmp_path):
    fake = FakeSol()
    server = await start(fake)
    store = TokenStore(tmp_path / "t.json")
    client = SolMateClient(SERIAL, PASSWORD, uri=uri(server), token_store=store)
    try:
        await client.start()
        live = await client.get_live_values()
        assert live.pv_w == 420.5 and live.soc_percent == 63.0 and live.temperature_c == 31.2
        s = await client.get_injection_settings()
        assert (s.min_w, s.max_w, s.min_battery_percent) == (50, 600, 20)
        await client.set_max_injection(450.7)
        assert fake.settings["user_maximum_injection"] == 450
        assert await client.is_online()
        assert store.get(f"cloud:{SERIAL}") == "TOKEN"
    finally:
        await client.stop()
        await server.close()


async def test_stored_token_skips_login(tmp_path):
    fake = FakeSol()
    server = await start(fake)
    store = TokenStore(tmp_path / "t.json")
    store.put(f"cloud:{SERIAL}", "TOKEN")
    client = SolMateClient(SERIAL, None, uri=uri(server), token_store=store)
    try:
        await client.start()
        assert [c[1] for c in fake.calls] == ["authenticate"]
    finally:
        await client.stop()
        await server.close()


async def test_follows_redirect(tmp_path):
    fake = FakeSol()
    server = await start(fake)
    fake.redirect_to = uri(server, "/node2")
    client = SolMateClient(SERIAL, PASSWORD, uri=uri(server))
    try:
        await client.start()
        await client.get_live_values()
        assert client.uri.endswith("/node2")
        assert fake.calls[-1][0] == "/node2" and fake.calls[-1][1] == "live_values"
    finally:
        await client.stop()
        await server.close()


async def test_wrong_password():
    server = await start(FakeSol())
    client = SolMateClient(SERIAL, "falsch", uri=uri(server))
    try:
        with pytest.raises(SolMateError):
            await client.start()
    finally:
        await client.stop()
        await server.close()


async def test_reconnects_after_drop():
    fake = FakeSol()
    server = await start(fake)
    client = SolMateClient(SERIAL, PASSWORD, uri=uri(server))
    try:
        await client.start()
        await client._ws.close()  # Verbindung bricht weg
        live = await client.get_live_values()
        assert live.inject_w == 300
    finally:
        await client.stop()
        await server.close()
