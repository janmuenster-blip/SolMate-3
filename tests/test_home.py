from datetime import date, datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from solmate_os.config import Config, HomeConfig, HomeDevice
from solmate_os.controller import Controller
from solmate_os.forecast import ForecastConfig, Plane, build_forecast
from solmate_os.home import HomeService, parse_device
from solmate_os.simulator import SimulatedSolMate
from solmate_os.storage import Storage
from solmate_os.tariff import Tariff, period_costs, year_projection
from solmate_os.waste import DEFAULT_TYPES, WasteCalendar, WasteRule, classify, parse_ics, summarize
from solmate_os.web import create_app

ICS = """BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261124\r
SUMMARY:Gelber Sack\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART:20261126T050000Z\r
SUMMARY:Abfuhr Papier\r
 tonne\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261123\r
RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4\r
EXDATE;VALUE=DATE:20261207\r
SUMMARY:Restmüll\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;VALUE=DATE:20261125\r
SUMMARY:Schadstoffmobil\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_parse_ics_and_classify():
    events = parse_ics(ICS, date(2027, 6, 1))
    by = {}
    for d, title in events:
        key = classify(title, DEFAULT_TYPES)
        if key:
            by.setdefault(key, []).append(d)
    assert by["gelb"] == [date(2026, 11, 24)]
    assert by["papier"] == [date(2026, 11, 26)]  # Zeilenumbruch (Folding) korrekt zusammengefügt
    assert by["rest"] == [date(2026, 11, 23), date(2026, 12, 21), date(2027, 1, 4)]  # EXDATE entfernt
    assert classify("Schadstoffmobil", DEFAULT_TYPES) is None


def test_waste_rules_and_summary():
    rule = WasteRule("gelb", date(2026, 11, 27), 2, {date(2026, 12, 25): date(2026, 12, 27), date(2027, 1, 8): None})
    ds = rule.dates(date(2026, 12, 1), date(2027, 1, 31))
    assert ds == [date(2026, 12, 11), date(2026, 12, 27), date(2027, 1, 22)]
    s = summarize({"gelb": ds, "rest": [date(2026, 12, 10)]}, DEFAULT_TYPES, date(2026, 12, 10))
    bins = {b["key"]: b for b in s["bins"]}
    assert bins["gelb"]["label"] == "Morgen" and bins["rest"]["label"] == "Heute"
    assert bins["papier"]["next"] is None
    assert s["reminder"] == "Heute Abend rausstellen: Gelber Sack"


async def test_waste_calendar_from_file(tmp_path):
    f = tmp_path / "abfall.ics"
    f.write_text(ICS, encoding="utf-8")
    cal = WasteCalendar(ics_files=[f])
    await cal.refresh(None, date(2026, 11, 22))
    s = cal.summary(date(2026, 11, 22))
    assert s["configured"] and not s["error"]
    assert {b["key"]: b["in_days"] for b in s["bins"]} == {"gelb": 2, "papier": 4, "rest": 1}


def test_parse_powerstream_and_stream():
    ps = parse_device(HomeDevice("HW51", kind="auto"), {
        "20_1.pv1InputWatts": 2105, "20_1.pv2InputWatts": 1890, "20_1.invOutputWatts": 3800, "20_1.batSoc": 0,
    })
    assert ps["kind"] == "powerstream"
    assert (ps["pv_w"], ps["pv1_w"], ps["out_w"], ps["soc_percent"]) == (399.5, 210.5, 380.0, None)
    st = parse_device(HomeDevice("BK31"), {"20_1.cmsBattSoc": 64, "gridConnectionPower": -250, "powGetPvSum": 0})
    assert (st["kind"], st["soc_percent"], st["out_w"]) == ("stream", 64, 250)


def test_forecast_build():
    cfg = ForecastConfig(48.8, 9.2, [Plane(0.8, 30, 0), Plane(0.4, 30, 90)], system_factor=1.0, max_ac_w=800)
    times = ["2026-11-22T11:00", "2026-11-22T12:00", "2026-11-23T12:00"]
    r1 = {"hourly": {"time": times, "global_tilted_irradiance": [500, 1000, 250], "temperature_2m": [15, 15, 15]},
          "current": {"temperature_2m": 6.1, "weather_code": 2},
          "daily": {"time": ["2026-11-22", "2026-11-23"], "weather_code": [2, 3], "temperature_2m_max": [8, 7],
                    "temperature_2m_min": [1, 2], "sunshine_duration": [7200, 0]}}
    r2 = {"hourly": {"time": times, "global_tilted_irradiance": [500, 1000, 250], "temperature_2m": [15, 15, 15]}}
    fc = build_forecast(cfg, [r1, r2], datetime(2026, 11, 22, 11, 0))
    # 12-Uhr-Wert: 1,2 kWp · 1000 W/m² → über 800 W → begrenzt; als 11:30 dargestellt
    assert fc["points"][1] == {"ts": "2026-11-22T11:30", "w": 800}
    assert fc["today_kwh"] > fc["tomorrow_kwh"] > 0
    assert fc["weather"]["text"] == "Teils bewölkt" and fc["days"][0]["sun_h"] == 2.0
    assert fc["rest_today_kwh"] == 0.8  # nur der Wert ab 11:30


def test_tariff_math():
    t = Tariff(price_ct_kwh=40, base_eur_month=15.2083, deposit_eur_month=100,
               contract_start=date(2026, 1, 1), meter_start_kwh=10000)
    c = period_costs(t, import_wh=2000, own_wh=3000, export_wh=500, days=1)
    assert c["energy_eur"] == 0.8 and c["saved_eur"] == 1.0 and c["base_eur"] == 0.5
    p = year_projection(t, [{"day": "2026-07-02", "kwh": 11000}], date(2026, 7, 10))
    assert p["per_day_kwh"] == round(1000 / 182, 2)
    # ~2005 kWh · 0,40 € + 182,50 € Grundpreis ≈ 984,70 € < 1200 € Abschläge → Guthaben
    assert p["balance_eur"] == 215.3 and p["fair_deposit_eur"] == 82.06
    assert year_projection(t, [], date(2026, 7, 10)) is None


async def test_home_api_simulator(tmp_path):
    cfg = Config()
    cfg.home = HomeConfig.from_dict({
        "waste": {"rules": [{"type": "rest", "start": date.today().isoformat(), "every_weeks": 2}]},
        "tariff": {"price_ct_kwh": 35, "base_eur_month": 12, "deposit_eur_month": 80,
                   "contract_start": (date.today() - timedelta(days=60)).isoformat(), "meter_start_kwh": 5000},
    })
    sim = SimulatedSolMate(seed=1, start_time=datetime(2026, 6, 1, 12, tzinfo=timezone.utc).astimezone())
    storage = Storage(tmp_path / "db")
    ctl = Controller(cfg, sim, storage)
    await ctl.tick()
    home = HomeService(cfg, storage, ctl)
    await home.waste.refresh(None, date.today())
    await home.forecast.refresh(None, home.power_clock())
    await home.poll_devices()
    async with TestClient(TestServer(create_app(ctl, home))) as client:
        body = await (await client.get("/api/home")).json()
        assert body["waste"]["bins"][2]["label"] == "Heute"
        names = [b["name"] for b in body["batteries"]]
        assert names == ["SolMate 3", "Stream AC Pro"]  # PowerStream ohne Akku nur in der Solar-Kachel
        assert body["solar"]["powerstreams"][0]["pv_w"] > 0
        assert body["solar"]["forecast"]["tomorrow_kwh"] > 0
        assert body["tariff"]["configured"] and body["tariff"]["projection"] is None
        r = await client.post("/api/home/meter", json={"kwh": 5300})
        body = await r.json()
        assert body["tariff"]["projection"]["year_kwh"] == round(300 / 60 * 365)
        assert (await client.post("/api/home/meter", json={"kwh": "x"})).status == 400
        r = await client.post("/api/home/meter", json={"day": date.today().isoformat(), "delete": True})
        assert (await r.json())["tariff"]["readings"] == []
        page = await (await client.get("/haus")).text()
        assert "Müllabfuhr" in page


STUTTGART_ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Sabre//Sabre VObject 4.5.8//EN
X-WR-CALNAME:Abfallwirtschaft Stuttgart
BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:DAYLIGHT
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
END:VTIMEZONE
BEGIN:VEVENT
SUMMARY:Restmüll 02-wöchentl.
DTSTART;TZID=Europe/Berlin:20261124T063000
DESCRIPTION:02-wöchentl.
BEGIN:VALARM
TRIGGER:-PT11H30M
DESCRIPTION:Restmüll
END:VALARM
END:VEVENT
BEGIN:VEVENT
SUMMARY:Altpapier 03-wöchentl.
DTSTART;TZID=Europe/Berlin:20261126T063000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Gelber Sack 03-wöchentl.
DTSTART;TZID=Europe/Berlin:20261203T063000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Biomüll 01-wöchentl.
DTSTART;TZID=Europe/Berlin:20261125T063000
END:VEVENT
END:VCALENDAR
"""


def test_stuttgart_format():
    """Format der Abfallwirtschaft Stuttgart (service.stuttgart.de/lhs-services/aws/api/ical)."""
    by = {}
    for d, title in parse_ics(STUTTGART_ICS, date(2027, 6, 1)):
        by.setdefault(classify(title, DEFAULT_TYPES), []).append(d)
    assert by == {"rest": [date(2026, 11, 24)], "papier": [date(2026, 11, 26)],
                  "gelb": [date(2026, 12, 3)], None: [date(2026, 11, 25)]}  # Bio nur, wenn eingerichtet
