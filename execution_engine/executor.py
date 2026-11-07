"""Order execution with idempotency and broker-confirmed results."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from connection_manager.mt5_gateway import Gateway
from database.db import Database
from risk_manager.risk import AccountState, Decision, RiskLimits, SymbolSpec, evaluate

# Common broker spellings for the same instrument. Always validated against the
# broker's real symbol list before use.
SYMBOL_ALIASES = {
    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSDm", "XAUUSD.a", "XAUUSD.", "XAUUSD#"],
    "EURUSD": ["EURUSD", "EURUSDm", "EURUSD.a", "EURUSD."],
}


def map_symbol(wanted: str, broker_symbols: set[str]) -> str | None:
    for cand in SYMBOL_ALIASES.get(wanted.upper(), [wanted]):
        if cand in broker_symbols:
            return cand
    return None


@dataclass
class Signal:
    key: str            # idempotency key, created once per strategy signal
    symbol: str
    is_buy: bool
    entry: Decimal
    stop: Decimal
    take_profit: Decimal | None
    requested_volume: Decimal | None = None


@dataclass
class ExecResult:
    status: str         # filled | rejected | duplicate | unknown | blocked
    detail: str
    position: int | None = None


class Executor:
    def __init__(self, db: Database, gw: Gateway, account_id: int, limits: RiskLimits,
                 magic: int, deviation: int = 20) -> None:
        self.db, self.gw, self.account_id = db, gw, account_id
        self.limits, self.magic, self.deviation = limits, magic, deviation
        self.entries_suspended = False

    def execute(self, sig: Signal, spec: SymbolSpec, state: AccountState,
                margin_per_lot: Decimal | None = None) -> ExecResult:
        # 1. Block while any earlier order of this account is unresolved.
        if self.db.unresolved_orders(self.account_id):
            return ExecResult("blocked", "unresolved earlier order; reconcile first")
        state.entries_suspended = state.entries_suspended or self.entries_suspended

        # 2. Risk gate, independent of the strategy.
        decision: Decision = evaluate(spec, self.limits, state, entry=sig.entry, stop=sig.stop,
                                      take_profit=sig.take_profit, is_buy=sig.is_buy,
                                      requested_volume=sig.requested_volume, margin_per_lot=margin_per_lot)
        if not decision.approved:
            self.db.event("trade_rejected", f"{sig.symbol}: {'; '.join(decision.reasons)}")
            return ExecResult("rejected", "; ".join(decision.reasons))

        # 3. Record intent BEFORE sending. A replayed signal stops here.
        if not self.db.begin_order(sig.key, self.account_id, sig.symbol, "buy" if sig.is_buy else "sell",
                                   str(decision.volume)):
            return ExecResult("duplicate", "signal already processed")

        # 4. Send and wait for the broker's answer.
        try:
            res = self.gw.send_market_order(sig.symbol, sig.is_buy, decision.volume, sig.stop,
                                            sig.take_profit, self.magic, f"ops-{sig.key[:8]}", self.deviation)
        except Exception as exc:  # noqa: BLE001 - connection lost mid-send
            self.db.set_order_state(sig.key, "UNKNOWN", detail=f"exception: {type(exc).__name__}")
            self.db.event("order_unknown", f"{sig.symbol} status unknown; will reconcile, not resend")
            return ExecResult("unknown", "no broker answer; reconcile before any retry")

        if res.unknown:
            self.db.set_order_state(sig.key, "UNKNOWN", detail=res.comment)
            return ExecResult("unknown", res.comment)
        if res.confirmed:
            self.db.set_order_state(sig.key, "FILLED", res.order, res.position)
            return ExecResult("filled", f"retcode {res.retcode}", res.position)
        self.db.set_order_state(sig.key, "REJECTED", detail=f"{res.retcode}: {res.comment}")
        self.db.event("broker_rejected", f"{sig.symbol}: {res.retcode} {res.comment}")
        return ExecResult("rejected", f"broker: {res.retcode} {res.comment}")

    def reconcile(self) -> int:
        """Resolve UNKNOWN/PENDING orders by matching broker positions on our comment tag.

        Returns the number still unresolved (entries stay blocked while > 0).
        """
        positions = self.gw.positions()
        pending = self.gw.pending_orders()
        for o in self.db.unresolved_orders(self.account_id):
            tag = f"ops-{o['idempotency_key'][:8]}"
            hit = next((p for p in positions + pending if str(p.get("comment", "")).startswith(tag)), None)
            if hit:
                self.db.set_order_state(o["idempotency_key"], "FILLED",
                                        broker_position=hit.get("ticket"), detail="found at broker on reconcile")
            else:
                # Not at broker: safe to mark rejected (it never arrived). Never auto-resent.
                self.db.set_order_state(o["idempotency_key"], "REJECTED",
                                        detail="not found at broker on reconcile; not resent")
        return len(self.db.unresolved_orders(self.account_id))
