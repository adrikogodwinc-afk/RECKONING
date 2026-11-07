"""Central risk checks, independent of any imported strategy."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN


@dataclass(frozen=True)
class SymbolSpec:
    tick_size: Decimal
    tick_value: Decimal      # value of one tick per 1.0 lot, in ACCOUNT currency
    volume_min: Decimal
    volume_max: Decimal
    volume_step: Decimal
    trade_allowed: bool = True


@dataclass
class RiskLimits:
    max_risk_money: Decimal              # hard cap per trade
    risk_percent: Decimal | None = None  # of equity, optional
    max_daily_loss: Decimal | None = None
    max_weekly_loss: Decimal | None = None
    max_drawdown_pct: Decimal | None = None
    max_consecutive_losses: int | None = None
    max_open_positions: int | None = None
    max_symbol_volume: Decimal | None = None
    max_total_volume: Decimal | None = None


@dataclass
class AccountState:
    equity: Decimal
    free_margin: Decimal
    peak_equity: Decimal
    daily_pnl: Decimal = Decimal(0)
    weekly_pnl: Decimal = Decimal(0)
    consecutive_losses: int = 0
    open_positions: int = 0
    symbol_volume: Decimal = Decimal(0)
    total_volume: Decimal = Decimal(0)
    emergency_stop: bool = False
    entries_suspended: bool = False


@dataclass
class Decision:
    approved: bool
    volume: Decimal = Decimal(0)
    risk_money: Decimal = Decimal(0)
    reasons: list[str] = field(default_factory=list)  # always logged, even on reject


def size_position(spec: SymbolSpec, entry: Decimal, stop: Decimal, risk_money: Decimal) -> Decimal:
    """Largest volume (rounded DOWN to the step) whose loss at the stop <= risk_money."""
    distance = abs(entry - stop)
    if distance <= 0 or spec.tick_size <= 0 or spec.tick_value <= 0:
        return Decimal(0)
    ticks = distance / spec.tick_size
    raw = risk_money / (ticks * spec.tick_value)
    steps = (raw / spec.volume_step).to_integral_value(rounding=ROUND_DOWN)
    return steps * spec.volume_step


def actual_risk(spec: SymbolSpec, entry: Decimal, stop: Decimal, volume: Decimal) -> Decimal:
    ticks = abs(entry - stop) / spec.tick_size
    return ticks * spec.tick_value * volume


def evaluate(spec: SymbolSpec, limits: RiskLimits, acct: AccountState, *, entry: Decimal,
             stop: Decimal, take_profit: Decimal | None, is_buy: bool,
             requested_volume: Decimal | None = None, margin_per_lot: Decimal | None = None) -> Decision:
    d = Decision(False)
    r = d.reasons

    if acct.emergency_stop:
        r.append("emergency stop active")
    if acct.entries_suspended:
        r.append("new entries suspended")
    if not spec.trade_allowed:
        r.append("trading not allowed for symbol")

    # SL / TP geometry
    if is_buy and not stop < entry:
        r.append("invalid SL: must be below entry for a buy")
    if (not is_buy) and not stop > entry:
        r.append("invalid SL: must be above entry for a sell")
    if take_profit is not None:
        if is_buy and not take_profit > entry:
            r.append("invalid TP: must be above entry for a buy")
        if (not is_buy) and not take_profit < entry:
            r.append("invalid TP: must be below entry for a sell")

    # Account-level limits
    if limits.max_daily_loss is not None and -acct.daily_pnl >= limits.max_daily_loss:
        r.append("daily loss limit reached")
    if limits.max_weekly_loss is not None and -acct.weekly_pnl >= limits.max_weekly_loss:
        r.append("weekly loss limit reached")
    if limits.max_drawdown_pct is not None and acct.peak_equity > 0:
        dd = (acct.peak_equity - acct.equity) / acct.peak_equity * 100
        if dd >= limits.max_drawdown_pct:
            r.append(f"drawdown {dd:.2f}% >= limit")
    if limits.max_consecutive_losses is not None and acct.consecutive_losses >= limits.max_consecutive_losses:
        r.append("consecutive loss limit reached")
    if limits.max_open_positions is not None and acct.open_positions >= limits.max_open_positions:
        r.append("max open positions reached")
    if r:
        return d

    # Risk budget: the tightest of the configured caps wins.
    budget = limits.max_risk_money
    if limits.risk_percent is not None:
        budget = min(budget, acct.equity * limits.risk_percent / 100)
    if budget <= 0:
        r.append("risk budget is zero")
        return d

    volume = size_position(spec, entry, stop, budget)
    if requested_volume is not None:
        # A strategy may ask for less, never more, than the platform allows.
        volume = min(volume, requested_volume) if requested_volume > 0 else Decimal(0)
        steps = (volume / spec.volume_step).to_integral_value(rounding=ROUND_DOWN)
        volume = steps * spec.volume_step
    if volume < spec.volume_min:
        r.append(f"required risk exceeds maximum: minimum lot {spec.volume_min} risks "
                 f"{actual_risk(spec, entry, stop, spec.volume_min):.2f} > budget {budget:.2f}")
        return d
    volume = min(volume, spec.volume_max)

    risk = actual_risk(spec, entry, stop, volume)
    if risk > limits.max_risk_money:
        r.append("computed risk exceeds max_risk_money")
        return d
    if margin_per_lot is not None and margin_per_lot * volume > acct.free_margin:
        r.append("insufficient free margin")
        return d
    if limits.max_symbol_volume is not None and acct.symbol_volume + volume > limits.max_symbol_volume:
        r.append("symbol exposure limit")
        return d
    if limits.max_total_volume is not None and acct.total_volume + volume > limits.max_total_volume:
        r.append("aggregate exposure limit")
        return d

    d.approved, d.volume, d.risk_money = True, volume, risk
    return d
