"""Thin wrapper over the official MetaTrader5 Python package.

Windows-only at runtime. The module imports cleanly elsewhere so that logic
above it can be unit-tested with a fake gateway. One isolated terminal process
per account: the MetaTrader5 package attaches to one terminal at a time, so each
account gets its own portable terminal folder and worker process.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

try:  # pragma: no cover - only available on Windows with MT5 installed
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # noqa: BLE001
    mt5 = None

TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_PLACED = 10008


@dataclass
class AccountInfo:
    login: int
    server: str
    balance: Decimal
    equity: Decimal
    margin_free: Decimal
    currency: str
    trade_allowed: bool


@dataclass
class OrderResult:
    confirmed: bool      # True ONLY when the broker returned a done/placed retcode
    retcode: int | None
    order: int | None
    position: int | None
    comment: str
    unknown: bool = False  # no answer received: caller must reconcile, never resend


class Gateway(Protocol):
    def connect(self, login: int, password: str, server: str, terminal_path: str | None) -> AccountInfo: ...
    def account_info(self) -> AccountInfo: ...
    def positions(self) -> list[dict]: ...
    def pending_orders(self) -> list[dict]: ...
    def send_market_order(self, symbol: str, is_buy: bool, volume: Decimal, sl: Decimal,
                          tp: Decimal | None, magic: int, comment: str, deviation: int) -> OrderResult: ...
    def shutdown(self) -> None: ...


class ConnectionFailed(Exception):
    pass


class MT5Gateway:
    def __init__(self) -> None:
        if mt5 is None:
            raise RuntimeError("MetaTrader5 package not available (Windows + MT5 terminal required)")

    def connect(self, login: int, password: str, server: str, terminal_path: str | None) -> AccountInfo:
        kwargs = {"login": login, "password": password, "server": server, "timeout": 30000}
        if terminal_path:
            kwargs["path"] = terminal_path
        if not mt5.initialize(**kwargs):
            code, msg = mt5.last_error()
            # message deliberately excludes the password
            raise ConnectionFailed(f"MT5 initialize failed ({code}): {msg}")
        info = self.account_info()
        # Verify we landed on the account and server that were requested.
        if info.login != login:
            mt5.shutdown()
            raise ConnectionFailed(f"Logged into {info.login}, expected {login}")
        if info.server.lower() != server.lower():
            mt5.shutdown()
            raise ConnectionFailed(f"Connected to server {info.server}, expected {server}")
        return info

    def account_info(self) -> AccountInfo:
        a = mt5.account_info()
        if a is None:
            raise ConnectionFailed("account_info unavailable")
        return AccountInfo(a.login, a.server, Decimal(str(a.balance)), Decimal(str(a.equity)),
                           Decimal(str(a.margin_free)), a.currency, bool(a.trade_allowed))

    def positions(self) -> list[dict]:
        pos = mt5.positions_get()
        if pos is None:
            raise ConnectionFailed("positions unavailable")
        return [p._asdict() for p in pos]

    def pending_orders(self) -> list[dict]:
        o = mt5.orders_get()
        if o is None:
            raise ConnectionFailed("orders unavailable")
        return [x._asdict() for x in o]

    def send_market_order(self, symbol, is_buy, volume, sl, tp, magic, comment, deviation) -> OrderResult:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return OrderResult(False, None, None, None, "no tick for symbol")
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if is_buy else tick.bid, "sl": float(sl), "deviation": deviation,
            "magic": magic, "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if tp is not None:
            req["tp"] = float(tp)
        res = mt5.order_send(req)
        if res is None:
            # No response at all: the order may or may not have reached the broker.
            return OrderResult(False, None, None, None, "no response", unknown=True)
        ok = res.retcode in (TRADE_RETCODE_DONE, TRADE_RETCODE_PLACED)
        return OrderResult(ok, res.retcode, res.order or None, getattr(res, "deal", None), res.comment)

    def shutdown(self) -> None:
        mt5.shutdown()


def find_terminals() -> list[str]:
    """Locate installed MT5 terminals (Windows)."""
    import glob
    import os
    if sys.platform != "win32":
        return []
    roots = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]
    found = []
    for r in roots:
        found += glob.glob(os.path.join(r, "*", "terminal64.exe"))
    return found
