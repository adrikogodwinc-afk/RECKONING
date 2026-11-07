from datetime import datetime, timezone, timedelta
from decimal import Decimal as D

import pytest

from ops_agent import power
from ops_agent.network_monitor import Failure, NetworkWatch, Probes, classify
from ops_agent.recovery import Action, Level, RecoveryEngine
from risk_manager.risk import AccountState, RiskLimits, SymbolSpec, evaluate, size_position


# ---------------- network ----------------
def probes(**over):
    base = dict(adapter_up=lambda: True, router_reachable=lambda: True, internet_reachable=lambda: True,
                dns_resolves=lambda: True, broker_reachable=lambda: True, api_reachable=lambda: True,
                tailscale_connected=lambda: True, mt5_connected=lambda: True)
    base.update(over)
    return Probes(**base)


@pytest.mark.parametrize("override,expected", [
    ({}, Failure.NONE),
    ({"adapter_up": lambda: False}, Failure.ADAPTER_DOWN),
    ({"router_reachable": lambda: False}, Failure.ROUTER_DOWN),
    ({"internet_reachable": lambda: False}, Failure.INTERNET_DOWN),
    ({"dns_resolves": lambda: False}, Failure.DNS_FAILURE),
    ({"broker_reachable": lambda: False}, Failure.BROKER_DOWN),
    ({"tailscale_connected": lambda: False}, Failure.TAILSCALE_DOWN),
    ({"mt5_connected": lambda: False}, Failure.MT5_DISCONNECTED),
])
def test_classification(override, expected):
    assert classify(probes(**override)) is expected


def test_lowest_layer_is_reported_first():
    p = probes(adapter_up=lambda: False, broker_reachable=lambda: False, mt5_connected=lambda: False)
    assert classify(p) is Failure.ADAPTER_DOWN


def test_outage_lifecycle_and_attempt_limit():
    events = []
    w = NetworkWatch(notify=lambda k, m: events.append(k), max_recovery_attempts=2, retry_interval_s=0)
    t = datetime.now(timezone.utc)
    assert w.observe(Failure.INTERNET_DOWN, t) == "attempt_recovery"
    assert w.observe(Failure.INTERNET_DOWN, t) == "attempt_recovery"
    assert w.observe(Failure.INTERNET_DOWN, t) == "give_up"
    assert w.observe(Failure.NONE, t + timedelta(seconds=90)) == "ok"
    assert events == ["outage", "recovered"]
    assert w.history[0].ended is not None


# ---------------- recovery ----------------
class FakeState:
    def __init__(self, fail_reconcile=False, dupes=0, fail_read=False):
        self.calls, self.fail_reconcile, self.dupes, self.fail_read = [], fail_reconcile, dupes, fail_read

    def component_state(self, c): self.calls.append("state"); return {}
    def open_positions(self, a):
        if self.fail_read:
            raise RuntimeError("MT5 unreachable")
        self.calls.append("positions"); return []
    def pending_orders(self, a): self.calls.append("orders"); return []
    def save_execution_state(self, c): self.calls.append("save"); return "snap1"
    def reconcile(self, a, s): self.calls.append("reconcile"); return not self.fail_reconcile
    def duplicate_orders_since(self, a, s): self.calls.append("dupes"); return self.dupes
    def suspend_new_entries(self, a): self.calls.append("suspend")
    def resume_new_entries(self, a): self.calls.append("resume")


def engine(state, ran):
    e = RecoveryEngine(state, notify=lambda k, m: None)
    e.register(Action("mt5_restart", Level.SAFE, lambda acct: ran.append(acct), trading_component=True, cooldown_s=0))
    return e


def test_restart_follows_protocol_order():
    st, ran = FakeState(), []
    res = engine(st, ran).execute("mt5_restart", account="A1")
    assert res.ok and ran == ["A1"]
    assert st.calls == ["state", "positions", "orders", "save", "suspend", "reconcile", "dupes", "resume"]


def test_restart_aborts_when_positions_unreadable():
    st, ran = FakeState(fail_read=True), []
    res = engine(st, ran).execute("mt5_restart", account="A1")
    assert not res.ok and res.needs_human and ran == []


def test_entries_stay_suspended_on_failed_reconcile_or_duplicates():
    for st in (FakeState(fail_reconcile=True), FakeState(dupes=2)):
        res = engine(st, []).execute("mt5_restart", account="A1")
        assert not res.ok and res.needs_human and "resume" not in st.calls


def test_no_mt5_restart_during_network_outage():
    st, ran = FakeState(), []
    res = engine(st, ran).execute("mt5_restart", account="A1", network_failure=Failure.INTERNET_DOWN)
    assert not res.ok and ran == [] and st.calls == []


def test_forbidden_and_unknown_actions():
    e = engine(FakeState(), [])
    with pytest.raises(ValueError):
        e.register(Action("place_trade", Level.SAFE, lambda: None))
    assert not e.execute("close_position", account="A1").ok
    assert not e.execute("run_shell").ok


def test_approval_and_disable_switch():
    e = RecoveryEngine(FakeState(), notify=lambda k, m: None)
    ran = []
    e.register(Action("api_restart", Level.NEEDS_APPROVAL, lambda: ran.append(1), cooldown_s=0))
    assert not e.execute("api_restart").ok and ran == []
    e.approvals = lambda n: True
    assert e.execute("api_restart").ok and ran == [1]
    e.enabled = False
    assert not e.execute("api_restart").ok


def test_attempt_limit_escalates():
    e = RecoveryEngine(FakeState(), notify=lambda k, m: None)

    def boom(): raise RuntimeError("x")
    e.register(Action("api_restart", Level.SAFE, boom, max_attempts=2, cooldown_s=0))
    assert not e.execute("api_restart").ok
    assert not e.execute("api_restart").ok
    assert e.execute("api_restart").detail == "attempt limit reached"


# ---------------- power ----------------
def test_wake_command_is_wellformed_and_rejects_unsafe_path():
    when = datetime(2026, 11, 9, 6, 45, tzinfo=timezone.utc)
    cmd = power.schedule_wake(when, r"C:\Program Files\TradingOps\agent.exe", dry_run=True)
    assert "-WakeToRun" in cmd[-1] and "Register-ScheduledTask" in cmd[-1]
    with pytest.raises(ValueError):
        power.build_wake_task_command(when, "C:\\x'; calc; '")
    with pytest.raises(ValueError):
        power.build_wake_task_command(datetime(2026, 11, 9, 6, 45), "C:\\a.exe")


def test_powercfg_parsing_modern_standby():
    out = ("The following sleep states are available on this system:\n"
           "    Standby (S0 Low Power Idle) Network Connected\n    Hibernate\n\n"
           "The following sleep states are not available on this system:\n"
           "    Standby (S3)\n        The system firmware does not support this standby state.\n")
    st = power.parse_powercfg_available(out)
    assert st["modern_standby"] and not st["s3_sleep"] and st["hibernate"]


def test_boot_classification():
    C = power.classify_previous_boot
    assert C(False, "sleep", 5, False) is power.PowerState.SLEEP
    assert C(False, "hibernate", 5, False) is power.PowerState.HIBERNATE
    assert C(True, None, 5, True) is power.PowerState.SHUTDOWN
    assert C(False, None, 5, False) is power.PowerState.UNEXPECTED_LOSS


def test_magic_packet_validation():
    with pytest.raises(ValueError):
        power.send_magic_packet("AA:BB")


def test_sleep_inhibitor_thread_lifecycle():
    inh = power.SleepInhibitor(interval=0.05)
    inh.start()
    assert inh.active
    inh.stop()
    assert not inh.active and inh.last_assert is not None


# ---------------- risk ----------------
GOLD = SymbolSpec(tick_size=D("0.01"), tick_value=D("0.01"), volume_min=D("0.01"),
                  volume_max=D("50"), volume_step=D("0.01"))
ACCT = AccountState(equity=D("10000"), free_margin=D("9000"), peak_equity=D("10000"))


def test_sizing_rounds_down_never_up():
    # $100 risk, $5 stop = 500 ticks * 0.01/tick/lot => $5/lot => 20 lots... use tighter spec
    vol = size_position(GOLD, D("2000.00"), D("1995.00"), D("100"))
    assert vol == D("20.00")
    vol = size_position(GOLD, D("2000.00"), D("1993.00"), D("100"))  # 700 ticks -> 14.2857
    assert vol == D("14.28")


def test_trade_rejected_when_minimum_lot_exceeds_budget():
    spec = SymbolSpec(D("0.01"), D("1"), D("0.01"), D("10"), D("0.01"))
    d = evaluate(spec, RiskLimits(max_risk_money=D("1")), ACCT, entry=D("2000"), stop=D("1990"),
                 take_profit=D("2020"), is_buy=True)
    assert not d.approved and "exceeds maximum" in d.reasons[0]


def test_strategy_cannot_request_more_than_platform_allows():
    lim = RiskLimits(max_risk_money=D("50"))
    d = evaluate(GOLD, lim, ACCT, entry=D("2000"), stop=D("1995"), take_profit=D("2010"),
                 is_buy=True, requested_volume=D("100"))
    assert d.approved and d.volume == D("10.00") and d.risk_money <= D("50")


def test_percent_cap_is_tighter_than_money_cap():
    lim = RiskLimits(max_risk_money=D("500"), risk_percent=D("0.5"))  # 0.5% of 10k = 50
    d = evaluate(GOLD, lim, ACCT, entry=D("2000"), stop=D("1995"), take_profit=None, is_buy=True)
    assert d.risk_money <= D("50")


@pytest.mark.parametrize("kw,needle", [
    (dict(stop=D("2005")), "invalid SL"),
    (dict(take_profit=D("1990")), "invalid TP"),
])
def test_invalid_sl_tp(kw, needle):
    args = dict(entry=D("2000"), stop=D("1995"), take_profit=D("2010"), is_buy=True)
    args.update(kw)
    d = evaluate(GOLD, RiskLimits(max_risk_money=D("50")), ACCT, **args)
    assert not d.approved and any(needle in x for x in d.reasons)


def test_account_limits_block_entries():
    lim = RiskLimits(max_risk_money=D("50"), max_daily_loss=D("200"), max_consecutive_losses=3,
                     max_drawdown_pct=D("10"), max_open_positions=2)
    base = dict(entry=D("2000"), stop=D("1995"), take_profit=D("2010"), is_buy=True)
    for field, val, needle in [("daily_pnl", D("-250"), "daily"), ("consecutive_losses", 3, "consecutive"),
                               ("equity", D("8900"), "drawdown"), ("open_positions", 2, "max open"),
                               ("emergency_stop", True, "emergency"), ("entries_suspended", True, "suspended")]:
        a = AccountState(equity=D("10000"), free_margin=D("9000"), peak_equity=D("10000"))
        setattr(a, field, val)
        d = evaluate(GOLD, lim, a, **base)
        assert not d.approved and any(needle in x for x in d.reasons), field


def test_insufficient_margin_and_exposure():
    lim = RiskLimits(max_risk_money=D("50"), max_symbol_volume=D("5"))
    base = dict(entry=D("2000"), stop=D("1995"), take_profit=D("2010"), is_buy=True)
    d = evaluate(GOLD, lim, ACCT, margin_per_lot=D("5000"), **base)
    assert not d.approved and "margin" in d.reasons[0]
    d = evaluate(GOLD, lim, ACCT, margin_per_lot=D("1"), **base)
    assert not d.approved and "symbol exposure" in d.reasons[0]
