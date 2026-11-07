import json
import pathlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from strategy_engine.sessions import Schedule

NY = ZoneInfo("America/New_York")
EXAMPLE = pathlib.Path(__file__).parent.parent / "strategy_engine" / "sessions.example.json"


def sched(confirmed=False):
    s = Schedule.from_file(str(EXAMPLE))
    s.confirmed = confirmed
    return s


def ny(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def test_user_sessions_flag_the_contained_session():
    warnings = sched().validate()
    assert any("fully inside" in w and "NY midday" in w for w in warnings)
    assert any("13h" in w for w in warnings)  # 07:00-20:00 flagged as possibly mistyped


def test_must_confirm_before_activation():
    s = sched()
    with pytest.raises(PermissionError):
        s.require_confirmed()
    s.confirm()
    s.require_confirmed()


def test_dst_offsets_follow_calendar_not_fixed_utc():
    s = sched()
    # Mon 2026-11-02 is EST (UTC-5); Mon 2026-03-09 is EDT (UTC-4)
    w_winter = s.next_session(ny(2026, 11, 2, 1, 0))
    w_summer = s.next_session(ny(2026, 3, 9, 1, 0))
    assert w_winter.start_utc.hour == 7   # 02:00 EST == 07:00 UTC
    assert w_summer.start_utc.hour == 6   # 02:00 EDT == 06:00 UTC


def test_spring_forward_gap_does_not_break():
    s = Schedule.from_dict({
        "sessions": [{"name": "early", "start": "02:00", "end": "05:00", "days": [0, 1, 2, 3, 4, 5, 6]}]
    })
    # 2026-03-08 02:00 NY does not exist; must still produce a valid window
    w = s.next_session(ny(2026, 3, 7, 12, 0))
    assert w is not None
    assert s.to_local(w.start_utc).date().isoformat() == "2026-03-08"
    assert w.end_utc > w.start_utc


def test_default_weekdays_skip_weekend():
    s = sched()
    # Saturday 2026-11-07 -> next session Monday 02:00 NY
    w = s.next_session(ny(2026, 11, 7, 9, 0))
    local = s.to_local(w.start_utc)
    assert local.weekday() == 0 and local.hour == 2


def test_wake_time_is_prep_minutes_before():
    s = sched()
    now = ny(2026, 11, 2, 1, 0)
    wake, window = s.next_wake(now)
    assert window.start_utc - wake == timedelta(minutes=15)
    s.prep_minutes = 30
    wake, window = s.next_wake(now)
    assert window.start_utc - wake == timedelta(minutes=30)


def test_active_and_trading_time():
    s = sched()
    assert s.is_trading_time(ny(2026, 11, 2, 13, 0))
    names = {x.name for x in s.active_sessions(ny(2026, 11, 2, 13, 0))}
    assert names == {"Main", "NY midday"}
    assert not s.is_trading_time(ny(2026, 11, 2, 6, 0))
    assert not s.is_trading_time(ny(2026, 11, 2, 20, 0))  # end exclusive


def test_merged_windows_collapse_overlap():
    s = sched()
    blocks = s.merged_windows(ny(2026, 11, 2, 0, 0), days=1)
    # 02-05 and 07-20 (with midday absorbed)
    assert len(blocks[:2]) == 2
    assert set(blocks[1][2]) == {"Main", "NY midday"}


def test_midnight_crossing_session():
    s = Schedule.from_dict({"sessions": [{"name": "night", "start": "22:00", "end": "02:00"}]})
    assert any("past midnight" in w for w in s.validate())
    assert s.is_trading_time(ny(2026, 11, 3, 1, 0))  # Monday-night session running Tue 01:00
