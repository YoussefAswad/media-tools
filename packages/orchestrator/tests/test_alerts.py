from __future__ import annotations

import datetime as dt

import orchestrator.alerts as alertsmod
from mediacore.audit import open_db
from orchestrator.alerts import CRIT, OK, WARN, AlertEngine, Evaluation, InAppChannel
from orchestrator.config import AlertRule

RULE = AlertRule(sustain_s=120, clear_s=60, cooldown_s=3600)


class Clock:
    def __init__(self) -> None:
        self.t = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def now(self) -> dt.datetime:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += dt.timedelta(seconds=seconds)


class Capture:
    def __init__(self) -> None:
        self.events: list = []

    def send(self, event) -> None:
        self.events.append(event)


def _engine(tmp_path, monkeypatch) -> tuple[AlertEngine, Clock]:
    clock = Clock()
    monkeypatch.setattr(alertsmod, "_now", clock.now)
    return AlertEngine(open_db(tmp_path / "state")), clock


def _ev(sev: str, rule: AlertRule = RULE) -> Evaluation:
    return Evaluation(key="disk:/d", label="D", severity=sev, detail=f"det {sev}", rule=rule)


def test_sustain_debounces_before_firing(tmp_path, monkeypatch) -> None:
    eng, clock = _engine(tmp_path, monkeypatch)
    cap = Capture()
    eng.evaluate([_ev(WARN)], [cap])
    assert cap.events == []  # not held long enough yet
    clock.advance(130)
    eng.evaluate([_ev(WARN)], [cap])
    assert len(cap.events) == 1
    assert cap.events[0].severity == WARN
    assert cap.events[0].kind == "alert"


def test_transient_spike_never_fires(tmp_path, monkeypatch) -> None:
    eng, clock = _engine(tmp_path, monkeypatch)
    cap = Capture()
    eng.evaluate([_ev(WARN)], [cap])
    clock.advance(30)  # shorter than sustain_s
    eng.evaluate([_ev(OK)], [cap])
    clock.advance(300)
    eng.evaluate([_ev(OK)], [cap])
    assert cap.events == []


def test_dedupe_and_cooldown_reminder(tmp_path, monkeypatch) -> None:
    eng, clock = _engine(tmp_path, monkeypatch)
    cap = Capture()
    eng.evaluate([_ev(WARN)], [cap])
    clock.advance(130)
    eng.evaluate([_ev(WARN)], [cap])  # fires
    clock.advance(60)
    eng.evaluate([_ev(WARN)], [cap])  # deduped (cooldown not elapsed)
    assert len(cap.events) == 1
    clock.advance(3600)
    eng.evaluate([_ev(WARN)], [cap])  # cooldown elapsed → reminder
    assert len(cap.events) == 2
    assert cap.events[1].kind == "reminder"


def test_escalation_warn_to_crit(tmp_path, monkeypatch) -> None:
    eng, clock = _engine(tmp_path, monkeypatch)
    cap = Capture()
    eng.evaluate([_ev(WARN)], [cap])
    clock.advance(130)
    eng.evaluate([_ev(WARN)], [cap])
    clock.advance(1)
    eng.evaluate([_ev(CRIT)], [cap])  # candidate crit, not yet sustained
    clock.advance(130)
    eng.evaluate([_ev(CRIT)], [cap])
    assert cap.events[-1].severity == CRIT
    assert cap.events[-1].kind == "escalate"


def test_recovery_after_clear_hold(tmp_path, monkeypatch) -> None:
    eng, clock = _engine(tmp_path, monkeypatch)
    cap = Capture()
    eng.evaluate([_ev(WARN)], [cap])
    clock.advance(130)
    eng.evaluate([_ev(WARN)], [cap])  # active=warn
    eng.evaluate([_ev(OK)], [cap])  # candidate ok, held 0
    assert eng.counts() == {"warn": 1}
    clock.advance(70)  # >= clear_s
    eng.evaluate([_ev(OK)], [cap])
    assert cap.events[-1].kind == "recovery"
    assert cap.events[-1].severity == OK
    assert eng.counts() == {}


def test_disabled_rule_does_not_fire(tmp_path, monkeypatch) -> None:
    eng, clock = _engine(tmp_path, monkeypatch)
    cap = Capture()
    disabled = AlertRule(sustain_s=0, enabled=False)
    eng.evaluate([_ev(CRIT, disabled)], [cap])
    clock.advance(1000)
    eng.evaluate([_ev(CRIT, disabled)], [cap])
    assert cap.events == []


def test_inapp_channel_logs_and_active_read(tmp_path, monkeypatch) -> None:
    eng, clock = _engine(tmp_path, monkeypatch)
    inapp = InAppChannel(eng.db)
    eng.evaluate([_ev(CRIT)], [inapp])
    clock.advance(130)
    eng.evaluate([_ev(CRIT)], [inapp])
    active = eng.active()
    assert len(active) == 1
    assert active[0].severity == CRIT
    assert active[0].label == "D"
    rows = eng.db.execute("SELECT COUNT(*) c FROM alert_events").fetchone()
    assert rows["c"] == 1
