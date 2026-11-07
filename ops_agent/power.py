"""Windows power management: keep the PC awake in sessions, wake it for the next.

Honest limits:
* Software cannot wake a PC that is fully shut down unless the BIOS/UEFI supports
  RTC alarm power-on or Wake-on-LAN. We only detect and report; we never claim it.
* Wake timers work from Sleep/Hibernate when "Allow wake timers" is enabled.
* Modern Standby (S0 low-power idle) laptops can be unreliable with wake timers.
"""
from __future__ import annotations

import ctypes
import logging
import socket
import subprocess
import sys
import threading
from datetime import datetime, timezone
from enum import Enum

log = logging.getLogger("ops.power")

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
WAKE_TASK_NAME = "TradingOps_Wake"
IS_WINDOWS = sys.platform == "win32"


class PowerState(str, Enum):
    RUNNING = "running"
    SLEEP = "sleep"
    HIBERNATE = "hibernate"
    SHUTDOWN = "shutdown"
    UNEXPECTED_LOSS = "unexpected_power_loss"


class SleepInhibitor:
    """Blocks system sleep while active.

    SetThreadExecutionState is per-thread, so a dedicated thread owns the flag
    and re-asserts it every ``interval`` seconds. If the process dies the flag
    dies with it, which is the safe failure direction (Windows may sleep, and
    the wake task plus watchdog notice).
    """

    def __init__(self, interval: float = 60.0) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_assert: datetime | None = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.active:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sleep-inhibitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self._assert()
            self._stop.wait(self.interval)
        self._release()

    def _assert(self) -> None:
        if IS_WINDOWS:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        self.last_assert = datetime.now(timezone.utc)

    def _release(self) -> None:
        if IS_WINDOWS:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)


def build_wake_task_command(wake_at_local: datetime, exe_path: str) -> list[str]:
    """PowerShell command registering a one-shot task that wakes the PC.

    ``wake_at_local`` must be timezone-aware; the task trigger uses the PC's
    local wall clock, so we convert to the system timezone first.
    """
    if wake_at_local.tzinfo is None:
        raise ValueError("wake_at_local must be timezone-aware")
    local = wake_at_local.astimezone()  # system local timezone
    stamp = local.strftime("%Y-%m-%dT%H:%M:%S")
    for ch in ('"', "'", ";", "`", "$"):
        if ch in exe_path:
            raise ValueError("unsafe character in executable path")
    ps = (
        f"$a = New-ScheduledTaskAction -Execute '{exe_path}' -Argument '--wake-check'; "
        f"$t = New-ScheduledTaskTrigger -Once -At ([datetime]'{stamp}'); "
        "$s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable "
        "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries; "
        f"Register-ScheduledTask -TaskName '{WAKE_TASK_NAME}' -Action $a -Trigger $t "
        "-Settings $s -RunLevel Limited -Force | Out-Null"
    )
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps]


def schedule_wake(wake_at: datetime, exe_path: str, dry_run: bool = False) -> list[str]:
    cmd = build_wake_task_command(wake_at, exe_path)
    if dry_run or not IS_WINDOWS:
        return cmd
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"Could not register wake task: {res.stderr.strip()[:300]}")
    log.info("Wake task registered for %s", wake_at.isoformat())
    return cmd


def parse_powercfg_available(output: str) -> dict:
    """Parse `powercfg /a` to see which sleep states exist.

    Modern Standby shows 'Standby (S0 Low Power Idle)'. Matching is done on the
    'available' section only, because unavailable states are listed afterwards.
    """
    avail, _, _ = output.partition("The following sleep states are not available")
    return {
        "s3_sleep": "Standby (S3)" in avail,
        "modern_standby": "S0 Low Power Idle" in avail,
        "hibernate": "Hibernate" in avail,
    }


def check_wake_readiness() -> dict:
    """Report whether the wake path is plausible on this machine."""
    if not IS_WINDOWS:
        return {"supported": False, "reason": "Not running on Windows"}
    out = subprocess.run(["powercfg", "/a"], capture_output=True, text=True, timeout=20).stdout
    states = parse_powercfg_available(out)
    issues = []
    if states["modern_standby"] and not states["s3_sleep"]:
        issues.append("Modern Standby: wake timers may be unreliable. Run the wake self-test.")
    if not states["hibernate"]:
        issues.append("Hibernate unavailable.")
    return {"supported": True, "states": states, "issues": issues}


def classify_previous_boot(clean_shutdown: bool, resumed_from: str | None, last_heartbeat_age_s: float,
                           expected_down: bool) -> PowerState:
    """Classify why the agent is starting, from signals gathered at boot.

    ``clean_shutdown``: Windows logged an orderly shutdown (Event ID 1074/6006).
    ``resumed_from``: 'sleep' / 'hibernate' if a resume event was logged.
    ``last_heartbeat_age_s``: seconds since last heartbeat before this start.
    ``expected_down``: we were deliberately off (outside session, no wake planned).
    """
    if resumed_from == "hibernate":
        return PowerState.HIBERNATE
    if resumed_from == "sleep":
        return PowerState.SLEEP
    if clean_shutdown:
        return PowerState.SHUTDOWN
    # No orderly shutdown and no resume: kernel power loss / crash (Event ID 41)
    return PowerState.UNEXPECTED_LOSS


def send_magic_packet(mac: str, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    """Wake-on-LAN. Run this from an always-on device (Pi/router/VPS-side relay).

    It only works if the target BIOS and NIC have Wake-on-LAN enabled, and
    realistically only over Ethernet.
    """
    hexmac = mac.replace(":", "").replace("-", "")
    if len(hexmac) != 12:
        raise ValueError("MAC address must be 6 bytes")
    payload = b"\xff" * 6 + bytes.fromhex(hexmac) * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(payload, (broadcast, port))
