from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from solmate_os.config import Config
from solmate_os.controller import Controller
from solmate_os.models import LiveValues
from solmate_os.simulator import SimulatedSolMate
from solmate_os.storage import Storage
from solmate_os.web import create_app


def test_energy_integration(tmp_path):
    st = Storage(tmp_path / "db")
    t0 = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    for i in range(61):  # eine Stunde konstant 400 W PV, 300 W Einspeisung, 100 W Laden
        st.add_sample(LiveValues(t0 + timedelta(minutes=i), 400, 300, 100, 50))
    e = st.energy(t0, t0 + timedelta(hours=2))
    assert e == {"pv_wh": 400.0, "inject_wh": 300.0, "charge_wh": 100.0, "discharge_wh": 0.0}


async def test_api_roundtrip(tmp_path):
    cfg = Config()
    sim = SimulatedSolMate(seed=1)
    ctl = Controller(cfg, sim, Storage(tmp_path / "db"))
    await ctl.tick()
    async with TestClient(TestServer(create_app(ctl))) as client:
        r = await client.get("/api/status")
        body = await r.json()
        assert body["online"] and body["live"]["soc_percent"] >= 0
        r = await client.post("/api/settings", json={"min_w": 100, "max_w": 500, "reserve_percent": 25, "minutes": 10})
        assert r.status == 200 and sim.settings.max_w == 500 and sim.settings.min_battery_percent == 25
        r = await client.post("/api/settings", json={"min_w": "x", "max_w": 500})
        assert r.status == 400
        r = await client.post("/api/boost", json={"watts": 600, "minutes": 5})
        assert r.status == 200
        assert (await client.get("/api/history")).status == 200
        assert (await client.get("/api/energy")).status == 200
        assert len(await (await client.get("/api/events")).json()) >= 1
        assert "SolMate OS" in await (await client.get("/")).text()
