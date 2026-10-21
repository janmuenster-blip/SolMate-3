from datetime import datetime, timedelta

from solmate_os.config import Config
from solmate_os.controller import Controller, decide
from solmate_os.models import LiveValues
from solmate_os.simulator import SimulatedSolMate
from solmate_os.storage import Storage

CFG = {
    "control": {
        "legal_limit_w": 800, "reserve_percent": 20, "default_min_w": 0, "default_max_w": 800,
        "max_temp_c": 60, "derate_w": 300,
        "schedule": [
            {"name": "Nacht", "start": "22:00", "end": "06:00", "min_w": 100, "max_w": 150},
            {"name": "Werktag", "start": "12:00", "end": "13:00", "min_w": 300, "max_w": 900,
             "weekdays": [0, 1, 2, 3, 4]},
        ],
    }
}


def at(day, hh, mm=0):  # 2026-10-19 ist ein Montag
    return datetime(2026, 10, 19 + day, hh, mm).astimezone()


def live(temp=30.0):
    return LiveValues(at(0, 12), 500, 300, 200, 50, temp)


def test_schedule_and_defaults():
    cfg = Config.from_dict(CFG)
    assert decide(cfg, at(0, 23), live()).reason == "Zeitplan „Nacht“"
    assert decide(cfg, at(1, 3), live()).min_w == 100          # nach Mitternacht
    assert decide(cfg, at(0, 8), live()).reason == "Standard"
    d = decide(cfg, at(0, 12, 30), live())
    assert (d.min_w, d.max_w) == (300, 800)                     # auf gesetzliche Grenze begrenzt
    assert decide(cfg, at(5, 12, 30), live()).reason == "Standard"  # Samstag


def test_overnight_rule_respects_weekday_of_start():
    cfg = Config.from_dict({"control": {"schedule": [
        {"name": "Fr-Nacht", "start": "22:00", "end": "06:00", "min_w": 50, "max_w": 100, "weekdays": [4]}]}})
    assert decide(cfg, at(4, 23), None).reason == "Zeitplan „Fr-Nacht“"   # Freitag 23 Uhr
    assert decide(cfg, at(5, 2), None).reason == "Zeitplan „Fr-Nacht“"    # Samstag 2 Uhr
    assert decide(cfg, at(5, 23), None).reason == "Standard"


def test_temperature_derate():
    cfg = Config.from_dict(CFG)
    d = decide(cfg, at(0, 8), live(temp=65))
    assert d.max_w == 300 and "Temperaturschutz" in d.reason


class Recorder(SimulatedSolMate):
    def __init__(self, **kw):
        super().__init__(seed=1, **kw)
        self.writes = []

    async def set_max_injection(self, w):
        self.writes.append(("max", w)); await super().set_max_injection(w)

    async def set_min_injection(self, w):
        self.writes.append(("min", w)); await super().set_min_injection(w)

    async def set_min_battery_percent(self, p):
        self.writes.append(("reserve", p)); await super().set_min_battery_percent(p)


async def test_tick_writes_only_changes(tmp_path):
    cfg = Config.from_dict(CFG)
    clock = [at(0, 23)]
    dev = Recorder()
    ctl = Controller(cfg, dev, Storage(tmp_path / "db"), clock=lambda: clock[0])
    await ctl.tick()
    assert dev.settings.max_w == 150 and dev.settings.min_w == 100 and dev.settings.min_battery_percent == 20
    # min muss nie über max liegen: beim Senken zuerst min, dann max
    assert dev.writes == [("min", 100), ("max", 150), ("reserve", 20)]
    dev.writes.clear()
    await ctl.tick()
    assert dev.writes == []                                   # nichts geändert → nichts gesendet
    clock[0] = at(1, 8)
    await ctl.tick()
    assert dev.writes == [("max", 800), ("min", 0)]


async def test_manual_override_pauses_automation(tmp_path):
    cfg = Config.from_dict(CFG)
    clock = [at(0, 8)]
    dev = Recorder()
    ctl = Controller(cfg, dev, Storage(tmp_path / "db"), clock=lambda: clock[0])
    await ctl.manual(200, 2000, 30, minutes=30)
    assert dev.settings.max_w == 800 and dev.settings.min_w == 200   # 2000 W gekappt
    await ctl.tick()
    assert dev.settings.min_w == 200
    clock[0] += timedelta(minutes=31)
    await ctl.tick()
    assert dev.settings.min_w == 0 and ctl.override_until is None


async def test_simulator_energy_balance():
    t = [0.0]
    sim = SimulatedSolMate(start_time=at(0, 11), speed=1, seed=3, clock=lambda: t[0], start_soc=50)
    await sim.set_max_injection(300)
    for _ in range(60):
        t[0] += 60
        v = await sim.get_live_values()
        assert v.inject_w <= 300 + 1e-6
        assert abs(v.pv_w - v.inject_w - v.battery_w) < 0.5 or v.soc_percent >= 100
    assert v.soc_percent > 50                                  # Mittag: Überschuss lädt die Batterie
