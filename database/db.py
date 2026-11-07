"""SQLite storage: accounts, bots, orders (idempotent), events, audit log."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY, login INTEGER NOT NULL, server TEXT NOT NULL, nickname TEXT,
  is_demo INTEGER NOT NULL DEFAULT 1, terminal_path TEXT, enabled INTEGER NOT NULL DEFAULT 0,
  credential_ref TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(login, server));
CREATE TABLE IF NOT EXISTS bots(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, language TEXT NOT NULL, source_path TEXT NOT NULL,
  config_json TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'stopped');
CREATE TABLE IF NOT EXISTS orders(
  id INTEGER PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE, account_id INTEGER NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, volume TEXT NOT NULL, state TEXT NOT NULL,
  broker_order INTEGER, broker_position INTEGER, detail TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT);
CREATE TABLE IF NOT EXISTS devices(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
"""

# Order states. UNKNOWN is never auto-resent: it must be reconciled with MT5 first.
ORDER_STATES = ("PENDING", "SUBMITTED", "FILLED", "REJECTED", "UNKNOWN")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path: str = "tradingops.db") -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # ---- accounts (passwords are NEVER stored here, only a vault reference) ----
    def add_account(self, login: int, server: str, nickname: str | None, is_demo: bool,
                    terminal_path: str | None, credential_ref: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO accounts(login,server,nickname,is_demo,terminal_path,credential_ref,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (login, server, nickname, int(is_demo), terminal_path, credential_ref, now()))
            return cur.lastrowid

    def accounts(self) -> list[dict]:
        with self.tx() as c:
            return [dict(r) for r in c.execute(
                "SELECT id,login,server,nickname,is_demo,terminal_path,enabled FROM accounts")]

    def set_account_enabled(self, account_id: int, enabled: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE accounts SET enabled=? WHERE id=?", (int(enabled), account_id))

    # ---- orders / idempotency ----
    @staticmethod
    def new_key() -> str:
        return uuid.uuid4().hex

    def begin_order(self, key: str, account_id: int, symbol: str, side: str, volume: str) -> bool:
        """Register intent BEFORE sending. Returns False if this signal was already seen."""
        try:
            with self.tx() as c:
                c.execute("INSERT INTO orders(idempotency_key,account_id,symbol,side,volume,state,created_at)"
                          " VALUES(?,?,?,?,?,'PENDING',?)", (key, account_id, symbol, side, volume, now()))
            return True
        except sqlite3.IntegrityError:
            return False

    def set_order_state(self, key: str, state: str, broker_order: int | None = None,
                        broker_position: int | None = None, detail: str | None = None) -> None:
        if state not in ORDER_STATES:
            raise ValueError(state)
        with self.tx() as c:
            c.execute("UPDATE orders SET state=?,broker_order=?,broker_position=?,detail=? WHERE idempotency_key=?",
                      (state, broker_order, broker_position, detail, key))

    def unresolved_orders(self, account_id: int) -> list[dict]:
        with self.tx() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM orders WHERE account_id=? AND state IN ('PENDING','SUBMITTED','UNKNOWN')",
                (account_id,))]

    def order(self, key: str) -> dict | None:
        with self.tx() as c:
            r = c.execute("SELECT * FROM orders WHERE idempotency_key=?", (key,)).fetchone()
            return dict(r) if r else None

    # ---- events / audit ----
    def event(self, kind: str, message: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events(ts,kind,message) VALUES(?,?,?)", (now(), kind, message))

    def recent_events(self, limit: int = 100) -> list[dict]:
        with self.tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

    def audit(self, actor: str, action: str, detail: str = "") -> None:
        with self.tx() as c:
            c.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)", (now(), actor, action, detail))

    # ---- devices ----
    def add_device(self, name: str, token_hash: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO devices(name,token_hash,created_at) VALUES(?,?,?)", (name, token_hash, now()))

    def device_valid(self, token_hash: str) -> bool:
        with self.tx() as c:
            r = c.execute("SELECT 1 FROM devices WHERE token_hash=? AND revoked=0", (token_hash,)).fetchone()
            return r is not None

    def revoke_device(self, token_hash: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE devices SET revoked=1 WHERE token_hash=?", (token_hash,))
