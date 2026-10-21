"""Kommandozeile: ``solmate-os run|status|set|boost``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import signal
from pathlib import Path

from aiohttp import web

from .backend import SolMateBackend
from .client import SolMateClient, TokenStore
from .config import Config
from .controller import Controller
from .simulator import SimulatedSolMate
from .storage import Storage
from .web import create_app


def make_backend(cfg: Config) -> SolMateBackend:
    if cfg.mode == "simulator":
        return SimulatedSolMate(
            cfg.serial,
            capacity_wh=cfg.sim_capacity_wh,
            pv_peak_w=cfg.sim_pv_peak_w,
            inverter_limit_w=cfg.legal_limit_w,
            speed=cfg.sim_speed,
        )
    return SolMateClient(
        cfg.serial,
        cfg.password,
        local=cfg.mode == "local",
        uri=cfg.uri,
        token_store=TokenStore(cfg.token_path),
    )


async def run_service(cfg: Config) -> None:
    backend = make_backend(cfg)
    storage = Storage(cfg.db_path)
    await backend.start()
    ctl = Controller(cfg, backend, storage)
    await ctl.start()
    runner = web.AppRunner(create_app(ctl))
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    logging.info("Weboberfläche: http://%s:%s", cfg.host, cfg.port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    await stop.wait()
    logging.info("Beende …")
    await runner.cleanup()
    await ctl.stop()
    await backend.stop()
    storage.close()


async def one_shot(cfg: Config, action: str, args: argparse.Namespace) -> None:
    backend = make_backend(cfg)
    await backend.start()
    try:
        if action == "status":
            live = await backend.get_live_values()
            settings = await backend.get_injection_settings()
            print(json.dumps({"live": live.to_dict(), "settings": settings.to_dict()}, indent=2, ensure_ascii=False))
        elif action == "set":
            if args.max is not None:
                await backend.set_max_injection(min(args.max, cfg.legal_limit_w))
            if args.min is not None:
                await backend.set_min_injection(min(args.min, cfg.legal_limit_w))
            if args.reserve is not None:
                await backend.set_min_battery_percent(args.reserve)
            print("OK")
        elif action == "boost":
            await backend.set_boost(int(args.minutes * 60), min(args.watts, cfg.legal_limit_w))
            print("OK")
    finally:
        await backend.stop()


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="solmate-os", description="Steuerung für den EET SolMate 3")
    p.add_argument("-c", "--config", type=Path, help="Pfad zur config.toml")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="Dienst mit Automatik und Weboberfläche starten")
    sub.add_parser("status", help="Aktuelle Werte einmalig ausgeben")
    s = sub.add_parser("set", help="Einspeisegrenzen direkt setzen")
    s.add_argument("--min", type=float)
    s.add_argument("--max", type=float)
    s.add_argument("--reserve", type=float, help="Batteriereserve in Prozent")
    b = sub.add_parser("boost", help="Boost-Einspeisung starten (0 Minuten = beenden)")
    b.add_argument("minutes", type=float)
    b.add_argument("watts", type=float)
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.load(args.config)
    if cfg.mode != "simulator" and not cfg.password and not TokenStore(cfg.token_path).get(
        f"{cfg.mode}:{cfg.serial}"
    ):
        cfg.password = getpass.getpass(f"Passwort für SolMate {cfg.serial}: ")

    if args.cmd == "run":
        asyncio.run(run_service(cfg))
    else:
        asyncio.run(one_shot(cfg, args.cmd, args))


if __name__ == "__main__":
    main()
