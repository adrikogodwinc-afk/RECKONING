"""Permission-based recovery interface.

The AI (or any caller) can only invoke actions registered here. There is no
shell access. Every trading-component restart goes through a mandatory
preflight/reconcile protocol; if any step cannot be verified, recovery stops
and the human is notified.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Protocol

from .network_monitor import Failure, NETWORK_LAYER_FAILURES


class Level(str, Enum):
    SAFE = "safe"                  # may run autonomously
    NEEDS_APPROVAL = "needs_approval"  # human must approve first
    FORBIDDEN = "forbidden"        # never exposed


# Things the agent must never do, regardless of who asks.
FORBIDDEN_ACTIONS = frozenset({
    "place_trade", "close_position", "modify_strategy", "change_risk_settings",
    "increase_lot_size", "disable_risk_protection", "change_credentials",
    "change_firewall", "disable_security", "run_shell",
})


class TradingState(Protocol):
    def component_state(self, component: str) -> dict: ...
    def open_positions(self, account: str) -> list[dict]: ...
    def pending_orders(self, account: str) -> list[dict]: ...
    def save_execution_state(self, component: str) -> str: ...
    def reconcile(self, account: str, snapshot_id: str) -> bool: ...
    def duplicate_orders_since(self, account: str, snapshot_id: str) -> int: ...
    def suspend_new_entries(self, account: str) -> None: ...
    def resume_new_entries(self, account: str) -> None: ...


@dataclass
class RecoveryResult:
    ok: bool
    action: str
    detail: str
    needs_human: bool = False


@dataclass
class Action:
    name: str
    level: Level
    run: Callable[..., None]
    trading_component: bool = False  # True -> full preflight/reconcile protocol
    max_attempts: int = 3
    cooldown_s: float = 60.0


class RecoveryEngine:
    def __init__(self, state: TradingState, notify: Callable[[str, str], None],
                 approvals: Callable[[str], bool] | None = None) -> None:
        self.state = state
        self.notify = notify
        self.approvals = approvals or (lambda name: False)
        self.enabled = True  # remote toggle from the phone
        self._actions: dict[str, Action] = {}
        self._attempts: dict[str, int] = {}
        self._last: dict[str, float] = {}

    def register(self, action: Action) -> None:
        if action.name in FORBIDDEN_ACTIONS:
            raise ValueError(f"'{action.name}' is forbidden and cannot be registered")
        self._actions[action.name] = action

    def available(self) -> list[str]:
        return sorted(self._actions)

    def execute(self, name: str, account: str | None = None,
                network_failure: Failure = Failure.NONE) -> RecoveryResult:
        if name in FORBIDDEN_ACTIONS or name not in self._actions:
            return RecoveryResult(False, name, "action not permitted", needs_human=True)
        act = self._actions[name]
        if not self.enabled:
            return RecoveryResult(False, name, "automated recovery disabled", needs_human=True)
        if act.level is Level.NEEDS_APPROVAL and not self.approvals(name):
            self.notify("approval_needed", f"'{name}' needs your approval")
            return RecoveryResult(False, name, "awaiting approval", needs_human=True)

        # Do not restart MT5/bots because the network is down.
        if act.trading_component and network_failure in NETWORK_LAYER_FAILURES:
            return RecoveryResult(False, name,
                                  f"skipped: {network_failure.value} (restart would not help)")

        key = f"{name}:{account}"
        if self._attempts.get(key, 0) >= act.max_attempts:
            self.notify("recovery_failed", f"{name} failed {act.max_attempts} times; manual action required")
            return RecoveryResult(False, name, "attempt limit reached", needs_human=True)
        if time.monotonic() - self._last.get(key, -1e9) < act.cooldown_s:
            return RecoveryResult(False, name, "cooldown active")
        self._attempts[key] = self._attempts.get(key, 0) + 1
        self._last[key] = time.monotonic()

        if act.trading_component:
            if account is None:
                return RecoveryResult(False, name, "account required", needs_human=True)
            result = self._guarded_restart(act, account)
        else:
            try:
                act.run()
                result = RecoveryResult(True, name, "done")
            except Exception as exc:  # noqa: BLE001 - report, never crash the agent
                result = RecoveryResult(False, name, f"error: {exc}", needs_human=True)

        if result.ok:
            self._attempts[key] = 0
            self.notify("recovery_ok", f"{name} succeeded")
        else:
            self.notify("recovery_failed", f"{name}: {result.detail}")
        return result

    def _guarded_restart(self, act: Action, account: str) -> RecoveryResult:
        """1 state, 2 positions, 3 orders, 4 save, 5 act, 6 reconcile, 7 duplicates."""
        s = self.state
        try:
            s.component_state(act.name)
            s.open_positions(account)     # failure to read => abort; never restart blind
            s.pending_orders(account)
        except Exception as exc:  # noqa: BLE001
            return RecoveryResult(False, act.name,
                                  f"cannot read positions/orders ({exc}); restart aborted",
                                  needs_human=True)
        snapshot = s.save_execution_state(act.name)
        s.suspend_new_entries(account)    # existing positions are left untouched
        try:
            act.run(account)
        except Exception as exc:  # noqa: BLE001
            return RecoveryResult(False, act.name, f"restart failed: {exc}", needs_human=True)
        if not s.reconcile(account, snapshot):
            return RecoveryResult(False, act.name,
                                  "reconciliation failed; new entries remain suspended",
                                  needs_human=True)
        dupes = s.duplicate_orders_since(account, snapshot)
        if dupes:
            return RecoveryResult(False, act.name,
                                  f"{dupes} duplicate order(s) detected; entries remain suspended",
                                  needs_human=True)
        s.resume_new_entries(account)
        return RecoveryResult(True, act.name, "restarted and reconciled")


# Recovery priorities: lower number is handled first.
PRIORITY = {
    "power_sleep_inhibit": 0,
    "network_recover": 1,
    "tailscale_reconnect": 2,
    "database_recover": 3,
    "api_restart": 4,
    "mt5_restart": 5,
    "bot_restart": 6,
}
