from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from solmate_os.config import Config, HomeConfig
from solmate_os.controller import Controller
from solmate_os.home import HomeService
from solmate_os.shelly import ShellyClient, ShellyDevice, parse_gen1, parse_gen2
from solmate_os.simulator import SimulatedSolMate
from solmate_os.storage import Storage

PRO_3EM = {"em:0": {"id": 0, "a_act_power": 120.5, "b_act_power": -40.0, "c_act_power": 300.0,
                    "total_act_power": 380.5}, "emdata:0": {"total_act": 123456.0}}


def test_parse_variants():
    assert parse_gen2(PRO_3EM) == {"power_w": 380.5, "phases_w": [120.5, -40.0, 300.0]}
    assert parse_gen2({"switch:0": {"apower": 1850.2, "output": True}})["power_w"] == 1850.2  # Plus Plug S
    assert parse_gen2({"em1:0": {"act_power": 200}, "em1:1": {"act_power": 50}})["power_w"] == 250  # Pro EM
    assert parse_gen2({"pm1:0": {"apower": 12}})["power_w"] == 12  # Plus PM Mini
    gen1_3em = {"emeters": [{"power": 100}, {"power": 200}, {"power": -50}], "total_power": 250}
    assert parse_gen1(gen1_3em) == {"power_w": 250, "phases_w": [100, 200, -50]}
    assert parse_gen1({"meters": [{"power": 75.5}]})["power_w"] == 75.5  # Plug S Gen1


async def _server(gen2: bool):
    async def shelly(_):
        return web.json_response({"id": "shellypro3em-x", "gen": 2} if gen2 else {"type": "SHEM-3"})

    async def rpc(_):
        return web.json_response(PRO_3EM)

    async def status(_):
        return web.json_response({"emeters": [{"power": -400}]})

    app = web.Application()
    app.router.add_get("/shelly", shelly)
    app.router.add_get("/rpc/Shelly.GetStatus", rpc)
    app.router.add_get("/status", status)
    server = TestServer(app)
    await server.start_server()
    return server


async def test_client_gen2_and_gen1_inverted():
    s2, s1 = await _server(True), await _server(False)
    try:
        async with aiohttp.ClientSession() as session:
            c2 = ShellyClient(ShellyDevice("Zähler", f"{s2.host}:{s2.port}", "grid"))
            assert (await c2.read(session))["power_w"] == 380.5 and c2.gen == 2
            c1 = ShellyClient(ShellyDevice("Alt", f"http://{s1.host}:{s1.port}", "grid", invert=True))
            assert (await c1.read(session))["power_w"] == 400  # Klemmen vertauscht → umgedreht
            plug = ShellyClient(ShellyDevice("Plug", f"{s1.host}:{s1.port}", "load"))
            assert (await plug.read(session))["power_w"] == 400  # Verbraucher immer positiv
    finally:
        await s1.close()
        await s2.close()


async def test_home_with_shelly_grid(tmp_path):
    server = await _server(True)
    try:
        cfg = Config()
        cfg.home = HomeConfig.from_dict({"shelly": [{"name": "Hauptzähler", "host": f"{server.host}:{server.port}",
                                                     "role": "grid"}]})
        sim = SimulatedSolMate(seed=2, start_time=datetime(2026, 6, 1, 12, tzinfo=timezone.utc).astimezone())
        storage = Storage(tmp_path / "db")
        ctl = Controller(cfg, sim, storage)
        await ctl.tick()
        home = HomeService(cfg, storage, ctl)
        home._session = aiohttp.ClientSession()
        try:
            await home.poll_shelly()
            await home.poll_devices()
            house = home.summary()["house"]
            own = ctl.last_live.inject_w + sum((s.get("out_w") or 0) for s in home.device_state.values())
            assert house["source"] == "shelly" and house["grid_w"] == 380.5
            assert house["load_w"] == round(380.5 + own, 1)
            assert house["phases_w"] == [120.5, -40.0, 300.0]
            assert home.has_grid_meter and home.summary()["consumers"] == []
        finally:
            await home._session.close()
    finally:
        await server.close()


def test_shelly_energy(tmp_path):
    st = Storage(tmp_path / "db")
    t0 = datetime(2026, 11, 22, 10, tzinfo=timezone.utc)
    for i in range(61):  # 30 min Bezug 1000 W, dann 30 min Einspeisung 200 W
        st.add_shelly_sample(t0 + timedelta(minutes=i), "Z", 1000.0 if i <= 30 else -200.0)
    e = st.shelly_energy("Z", t0)
    assert round(e["import_wh"]) == 500 + 7 and round(e["export_wh"]) == 100 - 3
