"""Layered network diagnosis.

Checks run bottom-up so the *first failing layer* is reported, which avoids
blaming MT5 or the broker for a dead WiFi link.
"""
from __future__ import annotations

import json
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable


class Failure(str, Enum):
    NONE = "ok"
    ADAPTER_DOWN = "wifi_or_adapter_disconnected"
    ROUTER_DOWN = "router_unavailable"
    INTERNET_DOWN = "internet_unavailable"
    DNS_FAILURE = "dns_failure"
    BROKER_DOWN = "broker_unavailable"
    API_DOWN = "central_api_unavailable"
    TAILSCALE_DOWN = "tailscale_disconnected"
    MT5_DISCONNECTED = "mt5_disconnected"  # network fine, terminal says no connection


@dataclass
class Probes:
    """Injectable probe functions so the logic is testable without a network."""
    adapter_up: Callable[[], bool]
    router_reachable: Callable[[], bool]
    internet_reachable: Callable[[], bool]   # raw IP, no DNS
    dns_resolves: Callable[[], bool]
    broker_reachable: Callable[[], bool]
    api_reachable: Callable[[], bool]
    tailscale_connected: Callable[[], bool]
    mt5_connected: Callable[[], bool | None]  # None = unknown/terminal not running


def classify(p: Probes) -> Failure:
    """Return the first failing layer."""
    if not p.adapter_up():
        return Failure.ADAPTER_DOWN
    if not p.router_reachable():
        return Failure.ROUTER_DOWN
    if not p.internet_reachable():
        return Failure.INTERNET_DOWN
    if not p.dns_resolves():
        return Failure.DNS_FAILURE
    if not p.broker_reachable():
        return Failure.BROKER_DOWN
    if not p.api_reachable():
        return Failure.API_DOWN
    if not p.tailscale_connected():
        return Failure.TAILSCALE_DOWN
    if p.mt5_connected() is False:
        return Failure.MT5_DISCONNECTED
    return Failure.NONE


# Failures where restarting MT5 is pointless: the network below it is broken.
NETWORK_LAYER_FAILURES = {
    Failure.ADAPTER_DOWN, Failure.ROUTER_DOWN, Failure.INTERNET_DOWN,
    Failure.DNS_FAILURE, Failure.BROKER_DOWN,
}


def tcp_probe(host: str, port: int, timeout: float = 3.0) -> tuple[bool, float]:
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, (time.monotonic() - t0) * 1000
    except OSError:
        return False, (time.monotonic() - t0) * 1000


def dns_probe(name: str = "example.com") -> bool:
    try:
        socket.getaddrinfo(name, 443)
        return True
    except socket.gaierror:
        return False


def tailscale_probe() -> bool:
    """True when the local Tailscale backend reports Running."""
    try:
        res = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=8)
        return json.loads(res.stdout).get("BackendState") == "Running"
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def latency_and_loss(host: str, port: int, samples: int = 5) -> dict:
    rtts, lost = [], 0
    for _ in range(samples):
        ok, ms = tcp_probe(host, port, 2.0)
        if ok:
            rtts.append(ms)
        else:
            lost += 1
    return {
        "avg_ms": round(sum(rtts) / len(rtts), 1) if rtts else None,
        "loss_pct": round(100 * lost / samples, 1),
    }


@dataclass
class Outage:
    kind: Failure
    started: datetime
    recovery_attempts: int = 0
    ended: datetime | None = None


@dataclass
class NetworkWatch:
    """Tracks outages, applies retry limits, and emits notifications."""
    notify: Callable[[str, str], None]
    max_recovery_attempts: int = 3
    retry_interval_s: float = 30.0
    current: Outage | None = None
    history: list[Outage] = field(default_factory=list)
    _last_attempt: float = 0.0

    def observe(self, failure: Failure, now: datetime | None = None) -> str:
        """Update state; returns 'ok', 'wait', 'attempt_recovery' or 'give_up'."""
        now = now or datetime.now(timezone.utc)
        if failure is Failure.NONE:
            if self.current:
                self.current.ended = now
                dur = (now - self.current.started).total_seconds()
                self.notify("recovered", f"{self.current.kind.value} resolved after {dur:.0f}s")
                self.history.append(self.current)
                self.current = None
            return "ok"
        if self.current is None or self.current.kind is not failure:
            if self.current:
                self.history.append(self.current)
            self.current = Outage(failure, now)
            self.notify("outage", f"{failure.value} started")
            self._last_attempt = 0.0
        if self.current.recovery_attempts >= self.max_recovery_attempts:
            return "give_up"  # caller escalates once; never loops forever
        if time.monotonic() - self._last_attempt < self.retry_interval_s and self._last_attempt:
            return "wait"
        self._last_attempt = time.monotonic()
        self.current.recovery_attempts += 1
        return "attempt_recovery"
