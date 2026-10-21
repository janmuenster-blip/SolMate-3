import hashlib
import hmac
from datetime import datetime, timedelta

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from solmate_os.config import Config
from solmate_os.controller import Controller
from solmate_os.ecoflow import EcoFlowClient, SimulatedStream, parse_state, sign
from solmate_os.simulator import SimulatedSolMate
from solmate_os.storage import Storage
from solmate_os.web import create_app


def test_sign_matches_reference():
    params = {"sn": "BK61X", "params": {"cfgBackupReverseSoc": 20, "flag": True}, "list": [1, 2]}
    expected_str = (
        "list[0]=1&list[1]=2&params.cfgBackupReverseSoc=20&params.flag=true&sn=BK61X"
        "&accessKey=AK&nonce=123456&timestamp=1700000000000"
    )
    expected = hmac.new(b"SK", expected_str.encode(), hashlib.sha256).hexdigest()
    assert sign("SK", "AK", params, "123456", "1700000000000") == expected


def test_parse_state_prefix_and_sign():
    stream = {"20_1.powGetPvSum": 500, "20_1.cmsBattSoc": 77, "gridConnectionPower": -300, "powGetSysLoad": 420}
    meter = {"powGetSysGrid": -120}
    s = parse_state(stream, meter, grid_import_positive=False)
    assert (s.pv_w, s.soc_percent, s.feed_in_w, s.load_w, s.grid_w) == (500, 77, 300, 420, 120)


async def test_client_signs_and_reads():
    seen = []

    async def quota(request):
        h = request.headers
        params = dict(request.query)
        expected = sign("SK", "AK", params, h["nonce"], h["timestamp"])
        seen.append(h["sign"] == expected)
        if h["sign"] != expected:
            return web.json_response({"code": "8521", "message": "signature is wrong"})
        data = {"BK61": {"powGetPvSum": 300, "cmsBattSoc": 55, "gridConnectionPower": -200},
                "BK21": {"powGetSysGrid": 42}}[params["sn"]]
        return web.json_response({"code": "0", "data": data})

    app = web.Application()
    app.router.add_get("/iot-open/sign/device/quota/all", quota)
    server = TestServer(app)
    await server.start_server()
    client = EcoFlowClient("AK", "SK", "BK61", "BK21", host=f"http://{server.host}:{server.port}")
    try:
        state = await client.get_state()
        assert seen == [True, True]
        assert (state.pv_w, state.soc_percent, state.feed_in_w, state.grid_w) == (300, 55, 200, 42)
    finally:
        await client.stop()
        await server.close()


def sim_pair(hour, sol_soc, stream_soc, strategy="balance"):
    t = [0.0]
    start = datetime(2026, 10, 19, hour).astimezone()
    sol = SimulatedSolMate(start_time=start, speed=1, seed=2, clock=lambda: t[0], start_soc=sol_soc)
    stream = SimulatedStream(sol, start_soc=stream_soc, seed=2, load_fn=lambda _t: 500.0)
    cfg = Config.from_dict({"control": {"interval_s": 30}, "ecoflow": {"mode": "simulator", "strategy": strategy}})
    return t, sol, stream, cfg


async def test_coordination_avoids_grid_and_respects_limit(tmp_path):
    t, sol, stream, cfg = sim_pair(21, sol_soc=80, stream_soc=40)
    ctl = Controller(cfg, sol, Storage(tmp_path / "db"), stream=stream)
    for _ in range(40):
        t[0] += 30
        await ctl.tick()
    s, live = ctl.last_stream, ctl.last_live
    assert ctl.solmate_leads                                  # SolMate-Akku voller → SolMate führt
    assert live.inject_w > 400                                # deckt den Großteil der 500 W
    assert abs(s.grid_w) < 60                                 # kaum Netzbezug, keine Einspeisung
    assert live.inject_w + s.feed_in_w <= cfg.household_limit_w + 1


async def test_stream_leads_when_fuller(tmp_path):
    t, sol, stream, cfg = sim_pair(21, sol_soc=30, stream_soc=90)
    ctl = Controller(cfg, sol, Storage(tmp_path / "db"), stream=stream)
    for _ in range(40):
        t[0] += 30
        await ctl.tick()
    assert not ctl.solmate_leads
    assert ctl.last_live.inject_w < 100 and ctl.last_stream.feed_in_w > 400


async def test_boost_is_capped_by_household_limit_and_ramps(tmp_path):
    t, sol, stream, cfg = sim_pair(21, sol_soc=80, stream_soc=90, strategy="stream_first")
    ctl = Controller(cfg, sol, Storage(tmp_path / "db"), stream=stream)
    for _ in range(5):
        t[0] += 30
        await ctl.tick()
    assert ctl.last_stream.feed_in_w > 400                    # Stream deckt das Haus
    await ctl.start_boost(20, 800)
    assert ctl.boost_sent_w < 800 and ctl.boost_note          # erst begrenzt …
    for _ in range(4):
        t[0] += 30
        await ctl.tick()
    assert ctl.boost_sent_w == 800                            # … Stream nimmt zurück, SolMate darf voll
    assert ctl.last_live.inject_w == 800
    t[0] += 21 * 60
    await ctl.tick()
    assert ctl.boost_until is None


async def test_boost_without_ecoflow_full_power(tmp_path):
    t = [0.0]
    sol = SimulatedSolMate(start_time=datetime(2026, 10, 19, 21).astimezone(), seed=1, clock=lambda: t[0], start_soc=80)
    ctl = Controller(Config(), sol, Storage(tmp_path / "db"))
    await ctl.tick()
    await ctl.start_boost(20, 800)
    t[0] += 30
    await ctl.tick()
    assert ctl.last_live.inject_w == 800
    assert ctl.status()["boost"]["total_s"] == 1200


async def test_pin_and_pwa_files(tmp_path):
    cfg = Config.from_dict({"web": {"pin": "4711"}})
    ctl = Controller(cfg, SimulatedSolMate(seed=1), Storage(tmp_path / "db"))
    await ctl.tick()
    async with TestClient(TestServer(create_app(ctl))) as client:
        assert (await client.get("/api/status")).status == 401
        assert (await client.get("/api/status", headers={"X-PIN": "4711"})).status == 200
        r = await client.get("/", allow_redirects=False)
        assert r.status == 302 and r.headers["Location"] == "/login"
        for path in ("/manifest.webmanifest", "/sw.js", "/icon-180.png", "/icon-512.png"):
            assert (await client.get(path)).status == 200, path
        assert (await client.post("/login", data={"pin": "0000"})).status == 401
        r = await client.post("/login", data={"pin": "4711"}, allow_redirects=False)
        assert r.status == 302
        assert (await client.get("/api/status")).status == 200
        r = await client.post("/api/boost", json={})
        body = await r.json()
        assert body["boost"]["requested_w"] == 800 and body["boost"]["total_s"] == 1200
