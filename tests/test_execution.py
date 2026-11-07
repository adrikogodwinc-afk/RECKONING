from decimal import Decimal as D

from connection_manager.mt5_gateway import OrderResult
from database.db import Database
from execution_engine.executor import Executor, Signal, map_symbol
from risk_manager.risk import AccountState, RiskLimits, SymbolSpec

SPEC = SymbolSpec(D("0.01"), D("0.01"), D("0.01"), D("50"), D("0.01"))


class FakeGW:
    def __init__(self, result=None, raises=False, positions=None):
        self.result, self.raises, self._positions, self.sent = result, raises, positions or [], 0

    def send_market_order(self, *a, **k):
        self.sent += 1
        if self.raises:
            raise ConnectionError("lost")
        return self.result

    def positions(self): return self._positions
    def pending_orders(self): return []


def make(tmp_path, gw):
    db = Database(str(tmp_path / "t.db"))
    aid = db.add_account(1, "S", None, True, None, "r")
    ex = Executor(db, gw, aid, RiskLimits(max_risk_money=D("50")), magic=7)
    return db, ex


def sig(key="k1"):
    return Signal(key, "XAUUSD", True, D("2000"), D("1995"), D("2010"))


def acct():
    return AccountState(equity=D("10000"), free_margin=D("9000"), peak_equity=D("10000"))


def test_fill_is_recorded_only_on_broker_confirmation(tmp_path):
    db, ex = make(tmp_path, FakeGW(OrderResult(True, 10009, 11, 22, "done")))
    r = ex.execute(sig(), SPEC, acct())
    assert r.status == "filled" and r.position == 22
    assert db.order("k1")["state"] == "FILLED"


def test_broker_rejection_is_not_success(tmp_path):
    db, ex = make(tmp_path, FakeGW(OrderResult(False, 10019, None, None, "no money")))
    assert ex.execute(sig(), SPEC, acct()).status == "rejected"
    assert db.order("k1")["state"] == "REJECTED"


def test_duplicate_signal_sends_once(tmp_path):
    gw = FakeGW(OrderResult(True, 10009, 1, 2, "ok"))
    db, ex = make(tmp_path, gw)
    ex.execute(sig(), SPEC, acct())
    assert ex.execute(sig(), SPEC, acct()).status == "duplicate"
    assert gw.sent == 1


def test_connection_loss_leaves_unknown_and_blocks_new_orders(tmp_path):
    gw = FakeGW(raises=True)
    db, ex = make(tmp_path, gw)
    assert ex.execute(sig("k1"), SPEC, acct()).status == "unknown"
    assert db.order("k1")["state"] == "UNKNOWN"
    # Replaying the same signal must NOT resend, and a new signal is blocked until reconciled.
    assert ex.execute(sig("k1"), SPEC, acct()).status == "blocked"
    assert ex.execute(sig("k2"), SPEC, acct()).status == "blocked"
    assert gw.sent == 1


def test_reconcile_finds_order_that_actually_filled(tmp_path):
    gw = FakeGW(raises=True)
    db, ex = make(tmp_path, gw)
    ex.execute(sig("abcdef123456"), SPEC, acct())
    gw._positions = [{"ticket": 555, "comment": "ops-abcdef12"}]
    assert ex.reconcile() == 0
    assert db.order("abcdef123456")["state"] == "FILLED"


def test_reconcile_marks_missing_order_without_resending(tmp_path):
    gw = FakeGW(raises=True)
    db, ex = make(tmp_path, gw)
    ex.execute(sig("zzz"), SPEC, acct())
    assert ex.reconcile() == 0
    assert db.order("zzz")["state"] == "REJECTED" and gw.sent == 1


def test_risk_rejection_never_reaches_broker(tmp_path):
    gw = FakeGW(OrderResult(True, 10009, 1, 2, "ok"))
    db, ex = make(tmp_path, gw)
    s = acct()
    s.emergency_stop = True
    assert ex.execute(sig(), SPEC, s).status == "rejected" and gw.sent == 0


def test_symbol_mapping():
    assert map_symbol("XAUUSD", {"GOLD", "EURUSD"}) == "GOLD"
    assert map_symbol("XAUUSD", {"XAUUSDm"}) == "XAUUSDm"
    assert map_symbol("XAUUSD", {"EURUSD"}) is None
