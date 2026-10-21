"""Weboberfläche (installierbare Web-App) und REST-API."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import timedelta
from pathlib import Path

from aiohttp import web

from .controller import Controller

STATIC = Path(__file__).parent / "static"
COOKIE = "solmate_os"
PUBLIC = {"/login", "/manifest.webmanifest", "/sw.js", "/icon-180.png", "/icon-192.png", "/icon-512.png"}


def _number(body: dict, key: str, default: float | None = None) -> float:
    value = body.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise web.HTTPBadRequest(text=f"'{key}' muss eine Zahl sein")
    return float(value)


def _token(pin: str) -> str:
    return hmac.new(pin.encode(), b"solmate-os-session", hashlib.sha256).hexdigest()


LOGIN_PAGE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="SolMate"><link rel="apple-touch-icon" href="/icon-180.png">
<link rel="manifest" href="/manifest.webmanifest"><title>SolMate – Anmelden</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;font:16px system-ui,sans-serif;
background:#12151b;color:#e7eaf0}form{width:min(320px,90vw);display:grid;gap:12px}
input,button{font:inherit;padding:14px;border-radius:12px;border:1px solid #2b3240}
input{background:#1b2029;color:inherit;text-align:center;font-size:24px;letter-spacing:.3em}
button{background:#5b9cf0;color:#fff;border:0;font-weight:600}p{color:#ef6b6b;text-align:center;margin:0}</style>
</head><body><form method="post" action="/login"><h1 style="text-align:center;margin:0 0 8px">SolMate</h1>
<input name="pin" type="password" inputmode="numeric" autocomplete="current-password" placeholder="PIN" autofocus required>
<button>Anmelden</button>__ERROR__</form></body></html>"""


def create_app(ctl: Controller) -> web.Application:
    routes = web.RouteTableDef()
    pin = ctl.cfg.pin

    @web.middleware
    async def auth(request: web.Request, handler):
        if pin and request.path not in PUBLIC:
            by_cookie = hmac.compare_digest(request.cookies.get(COOKIE, ""), _token(pin))
            by_header = hmac.compare_digest(request.headers.get("X-PIN", ""), pin)  # z. B. iPhone-Kurzbefehl
            if not (by_cookie or by_header):
                if request.path.startswith("/api/"):
                    raise web.HTTPUnauthorized(text="PIN erforderlich")
                raise web.HTTPFound("/login")
        response = await handler(request)
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @routes.get("/login")
    async def login_form(_: web.Request) -> web.Response:
        if not pin:
            raise web.HTTPFound("/")
        return web.Response(text=LOGIN_PAGE.replace("__ERROR__", ""), content_type="text/html")

    @routes.post("/login")
    async def login(req: web.Request) -> web.Response:
        form = await req.post()
        if pin and hmac.compare_digest(str(form.get("pin", "")), pin):
            resp = web.HTTPFound("/")
            resp.set_cookie(COOKIE, _token(pin), max_age=400 * 24 * 3600, httponly=True, samesite="Strict")
            raise resp
        await asyncio.sleep(1.5)  # bremst Durchprobieren
        return web.Response(
            text=LOGIN_PAGE.replace("__ERROR__", "<p>Falsche PIN</p>"), content_type="text/html", status=401
        )

    @routes.get("/")
    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/manifest.webmanifest")
    async def manifest(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "manifest.webmanifest", headers={"Content-Type": "application/manifest+json"})

    @routes.get("/sw.js")
    async def service_worker(_: web.Request) -> web.FileResponse:
        return web.FileResponse(
            STATIC / "sw.js", headers={"Content-Type": "text/javascript", "Cache-Control": "no-cache"}
        )

    @routes.get("/icon-{size:(180|192|512)}.png")
    async def icon(req: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / f"icon-{req.match_info['size']}.png")

    @routes.get("/api/status")
    async def status(_: web.Request) -> web.Response:
        return web.json_response(ctl.status())

    @routes.get("/api/history")
    async def history(req: web.Request) -> web.Response:
        hours = min(float(req.query.get("hours", 24)), 24 * 31)
        since = ctl.clock() - timedelta(hours=hours)
        return web.json_response(
            {"solmate": ctl.storage.history(since), "stream": ctl.storage.stream_history(since)}
        )

    @routes.get("/api/energy")
    async def energy(_: web.Request) -> web.Response:
        # Gerätezeit verwenden (im Simulator läuft die Uhr ggf. schneller).
        midnight = ctl.clock().replace(hour=0, minute=0, second=0, microsecond=0)
        week = midnight - timedelta(days=6)
        return web.json_response(
            {
                "today": {**ctl.storage.energy(midnight), **ctl.storage.grid_energy(midnight)},
                "last_7_days": {**ctl.storage.energy(week), **ctl.storage.grid_energy(week)},
            }
        )

    @routes.get("/api/events")
    async def events(_: web.Request) -> web.Response:
        return web.json_response(ctl.storage.events())

    @routes.post("/api/automatic")
    async def automatic(req: web.Request) -> web.Response:
        body = await req.json()
        ctl.set_automatic(bool(body.get("enabled")))
        return web.json_response(ctl.status())

    @routes.post("/api/settings")
    async def settings(req: web.Request) -> web.Response:
        body = await req.json()
        await ctl.manual(
            _number(body, "min_w"),
            _number(body, "max_w"),
            _number(body, "reserve_percent", ctl.cfg.reserve_percent),
            int(_number(body, "minutes", 60)),
        )
        return web.json_response(ctl.status())

    @routes.post("/api/boost")
    async def boost(req: web.Request) -> web.Response:
        body = await req.json() if req.can_read_body else {}
        minutes = _number(body, "minutes", ctl.cfg.boost_minutes)
        watts = _number(body, "watts", ctl.cfg.boost_watts)
        await ctl.start_boost(minutes, watts)
        return web.json_response(ctl.status())

    app = web.Application(middlewares=[auth])
    app.add_routes(routes)
    return app
