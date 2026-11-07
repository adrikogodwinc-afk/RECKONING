"""Trading session scheduler.

All session times are wall-clock times in an IANA timezone (default
America/New_York). Conversion to UTC always goes through zoneinfo, never a
fixed offset, so daylight saving changes are handled automatically.

Sessions are plain editable config (see ``sessions.example.json``). A schedule
must be validated and explicitly confirmed before it can be activated.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, date, time, timedelta, timezone
from itertools import combinations
from typing import Iterable
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/New_York"
DEFAULT_PREP_MINUTES = 15
DEFAULT_DAYS = (0, 1, 2, 3, 4)  # Monday..Friday, editable per session
LONG_SESSION_HOURS = 10


def _parse_hhmm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Session:
    name: str
    start: time
    end: time
    days: tuple[int, ...] = DEFAULT_DAYS
    prep_minutes: int | None = None  # None -> schedule default

    @property
    def crosses_midnight(self) -> bool:
        return self.end <= self.start

    def minutes(self) -> tuple[int, int]:
        s = self.start.hour * 60 + self.start.minute
        e = self.end.hour * 60 + self.end.minute
        if e <= s:
            e += 24 * 60
        return s, e


@dataclass(frozen=True)
class Window:
    """One concrete occurrence of a session, in UTC."""
    session: Session
    start_utc: datetime
    end_utc: datetime


@dataclass
class Schedule:
    sessions: list[Session]
    tz_name: str = DEFAULT_TZ
    prep_minutes: int = DEFAULT_PREP_MINUTES
    confirmed: bool = False
    _tz: ZoneInfo = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._tz = ZoneInfo(self.tz_name)

    # ---------- config ----------
    @classmethod
    def from_dict(cls, cfg: dict) -> "Schedule":
        sessions = [
            Session(
                name=s["name"],
                start=_parse_hhmm(s["start"]),
                end=_parse_hhmm(s["end"]),
                days=tuple(s.get("days", DEFAULT_DAYS)),
                prep_minutes=s.get("prep_minutes"),
            )
            for s in cfg["sessions"]
        ]
        return cls(
            sessions=sessions,
            tz_name=cfg.get("timezone", DEFAULT_TZ),
            prep_minutes=cfg.get("prep_minutes", DEFAULT_PREP_MINUTES),
            confirmed=cfg.get("confirmed", False),
        )

    @classmethod
    def from_file(cls, path: str) -> "Schedule":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    # ---------- validation ----------
    def validate(self) -> list[str]:
        """Return human-readable warnings. Empty list means nothing suspicious."""
        warnings: list[str] = []
        for s in self.sessions:
            if not s.days:
                warnings.append(f"'{s.name}' has no trading days.")
            if s.crosses_midnight:
                warnings.append(f"'{s.name}' ends at/before it starts, so it runs past midnight. Confirm this is intended.")
            a, b = s.minutes()
            if (b - a) / 60 >= LONG_SESSION_HOURS:
                warnings.append(
                    f"'{s.name}' is {(b - a) / 60:.0f}h long ({s.start:%H:%M}-{s.end:%H:%M}). Possible typo?"
                )
        for x, y in combinations(self.sessions, 2):
            if not set(x.days) & set(y.days):
                continue
            ax, bx = x.minutes()
            ay, by = y.minutes()
            if ax < by and ay < bx:
                kind = "is fully inside" if (ax >= ay and bx <= by) or (ay >= ax and by <= bx) else "overlaps"
                warnings.append(
                    f"'{x.name}' ({x.start:%H:%M}-{x.end:%H:%M}) {kind} "
                    f"'{y.name}' ({y.start:%H:%M}-{y.end:%H:%M})."
                )
        return warnings

    def confirm(self) -> None:
        self.confirmed = True

    def require_confirmed(self) -> None:
        if not self.confirmed:
            raise PermissionError(
                "Schedule not confirmed. Review validate() warnings, then call confirm()."
            )

    # ---------- time maths ----------
    def _local_to_utc(self, d: date, t: time) -> datetime:
        wall = datetime.combine(d, t, tzinfo=self._tz)
        # Round-trip through UTC: a non-existent wall time (spring-forward gap)
        # is normalised to the real instant rather than left invalid.
        return wall.astimezone(timezone.utc)

    def windows(self, start_utc: datetime, days: int = 8) -> list[Window]:
        """All session windows that end after ``start_utc``, within a horizon."""
        start_utc = start_utc.astimezone(timezone.utc)
        first_day = start_utc.astimezone(self._tz).date() - timedelta(days=1)
        out: list[Window] = []
        for i in range(days + 2):
            d = first_day + timedelta(days=i)
            for s in self.sessions:
                if d.weekday() not in s.days:
                    continue
                w_start = self._local_to_utc(d, s.start)
                end_day = d + timedelta(days=1) if s.crosses_midnight else d
                w_end = self._local_to_utc(end_day, s.end)
                if w_end > start_utc:
                    out.append(Window(s, w_start, w_end))
        out.sort(key=lambda w: (w.start_utc, w.end_utc))
        return out

    def merged_windows(self, start_utc: datetime, days: int = 8) -> list[tuple[datetime, datetime, list[str]]]:
        """Overlapping windows collapsed into continuous blocks."""
        merged: list[tuple[datetime, datetime, list[str]]] = []
        for w in self.windows(start_utc, days):
            if merged and w.start_utc <= merged[-1][1]:
                s, e, names = merged[-1]
                merged[-1] = (s, max(e, w.end_utc), names + [w.session.name])
            else:
                merged.append((w.start_utc, w.end_utc, [w.session.name]))
        return merged

    def active_sessions(self, now: datetime) -> list[Session]:
        now = now.astimezone(timezone.utc)
        return [w.session for w in self.windows(now, 2) if w.start_utc <= now < w.end_utc]

    def is_trading_time(self, now: datetime) -> bool:
        return bool(self.active_sessions(now))

    def next_session(self, now: datetime) -> Window | None:
        """First window that starts strictly after ``now``."""
        now = now.astimezone(timezone.utc)
        for w in self.windows(now, 9):
            if w.start_utc > now:
                return w
        return None

    def prep_for(self, s: Session) -> int:
        return self.prep_minutes if s.prep_minutes is None else s.prep_minutes

    def wake_time(self, window: Window) -> datetime:
        """When the PC must be awake so infrastructure can be verified."""
        return window.start_utc - timedelta(minutes=self.prep_for(window.session))

    def next_wake(self, now: datetime) -> tuple[datetime, Window] | None:
        """Next wake instant strictly in the future, with the window it serves.

        If a session is currently active the agent is already awake; the next
        wake is for the following block (skipping windows already running).
        """
        now = now.astimezone(timezone.utc)
        for w in self.windows(now, 9):
            wake = self.wake_time(w)
            if wake > now:
                return wake, w
        return None

    def to_local(self, dt: datetime) -> datetime:
        return dt.astimezone(self._tz)
