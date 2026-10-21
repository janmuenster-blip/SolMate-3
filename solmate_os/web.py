"""Weboberfläche und REST-API."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from aiohttp import web

from .controller import Controller

STATIC = Path(__file__).parent / "static"


def _number(body: dict, key: str, default: float | None = None) -> float:
    value = body.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise web.HTTPBadRequest(text=f"'{key}' muss eine Zahl sein")
    return float(value)


def create_app(ctl: Controller) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    @routes.get("/api/status")
    async def status(_: web.Request) -> web.Response:
        return web.json_response(ctl.status())

    @routes.get("/api/history")
    async def history(req: web.Request) -> web.Response:
        hours = min(float(req.query.get("hours", 24)), 24 * 31)
        since = ctl.clock() - timedelta(hours=hours)
        return web.json_response(ctl.storage.history(since))

    @routes.get("/api/energy")
    async def energy(_: web.Request) -> web.Response:
        # Gerätezeit verwenden (im Simulator läuft die Uhr ggf. schneller).
        midnight = ctl.clock().replace(hour=0, minute=0, second=0, microsecond=0)
        return web.json_response(
            {
                "today": ctl.storage.energy(midnight),
                "last_7_days": ctl.storage.energy(midnight - timedelta(days=6)),
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
        body = await req.json()
        await ctl.boost(int(_number(body, "minutes", 0) * 60), _number(body, "watts", 0))
        return web.json_response({"ok": True})

    app = web.Application()
    app.add_routes(routes)
    return app
