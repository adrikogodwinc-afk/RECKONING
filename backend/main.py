"""TradingOps backend: FastAPI server + ops agent loop. Entry point of the Windows EXE."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from database.db import Database
from ops_agent import power
from ops_agent.network_monitor import Failure, Probes, classify, dns_probe, tailscale_probe, tcp_probe
from security.vault import Pairing, Vault, hash_token
from strategy_engine.sessions import Schedule

APP_DIR = Path(os.environ.get("TRADINGOPS_HOME", Path(os.environ.get("APPDATA", Path.home())) / "TradingOps"))
APP_DIR.mkdir(parents=True, exist_ok=True)
BASE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))

db = Database(str(APP_DIR / "tradingops.db"))
vault = Vault(str(APP_DIR / "vault"))
pairing = Pairing()
inhibitor = power.SleepInhibitor()

SCHEDULE_FILE = APP_DIR / "sessions.json"
if not SCHEDULE_FILE.exists():
    SCHEDULE_FILE.write_text((BASE / "strategy_engine" / "sessions.example.json").read_text(), encoding="utf-8")
schedule = Schedule.from_file(str(SCHEDULE_FILE))

STATE: dict = {"internet": None, "failure": "unknown", "recovery_enabled": True,
               "entries_suspended": False, "last_health_ok": None, "session": None,
               "sleep_blocked": False}

app = FastAPI(title="TradingOps", docs_url=None, redoc_url=None, openapi_url=None)
sockets: set[WebSocket] = set()


def is_local(request: Request) -> bool:
    return request.client is not None and request.client.host in ("127.0.0.1", "::1")


def auth(authorization: str = Header(default="")) -> str:
    token = authorization.removeprefix("Bearer ").strip()
    if not token or not db.device_valid(hash_token(token)):
        raise HTTPException(401, "unauthorized")
    return hash_token(token)[:8]


def local_only(request: Request) -> None:
    if not is_local(request):
        raise HTTPException(403, "this action is only allowed on the PC itself")


# ---------------- public ----------------
@app.get("/health")
def health():
    return {"ok": True}


class PairBody(BaseModel):
    code: str
    device_name: str = "phone"


@app.post("/pair")
def pair(body: PairBody):
    token = pairing.redeem(body.code.strip())
    if not token:
        raise HTTPException(400, "invalid or expired code")
    db.add_device(body.device_name[:40], hash_token(token))
    db.audit("system", "device_paired", body.device_name[:40])
    return {"token": token}


# ---------------- PC-only (localhost) ----------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    local_only(request)
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8") if (
        Path(__file__).parent / "dashboard.html").exists() else (BASE / "backend" / "dashboard.html").read_text(encoding="utf-8")


@app.post("/local/pairing-code")
def new_code(request: Request):
    local_only(request)
    return {"code": pairing.new_code(), "valid_seconds": pairing.ttl_s}


class AccountBody(BaseModel):
    login: int
    password: str
    server: str
    nickname: str | None = None
    is_demo: bool = True
    terminal_path: str | None = None


@app.post("/local/accounts")
def add_account(body: AccountBody, request: Request):
    """Passwords are accepted only from the PC itself and go straight into the DPAPI vault."""
    local_only(request)
    ref = vault.store(body.password)
    try:
        aid = db.add_account(body.login, body.server, body.nickname, body.is_demo, body.terminal_path, ref)
    except Exception:
        vault.revoke(ref)
        raise HTTPException(400, "account already exists or invalid")
    db.audit("local", "account_added", f"{body.login}@{body.server}")
    return {"id": aid}  # password is never echoed back


@app.get("/local/schedule")
def get_schedule(request: Request):
    local_only(request)
    return {"timezone": schedule.tz_name, "confirmed": schedule.confirmed,
            "warnings": schedule.validate(), "raw": json.loads(SCHEDULE_FILE.read_text())}


@app.post("/local/schedule/confirm")
def confirm_schedule(request: Request):
    local_only(request)
    cfg = json.loads(SCHEDULE_FILE.read_text())
    cfg["confirmed"] = True
    SCHEDULE_FILE.write_text(json.dumps(cfg, indent=2))
    schedule.confirm()
    db.audit("local", "schedule_confirmed", "; ".join(schedule.validate()))
    return {"confirmed": True}


# ---------------- authenticated (phone) ----------------
def status_payload() -> dict:
    now = datetime.now(timezone.utc)
    nxt = schedule.next_wake(now)
    return {
        "time_utc": now.isoformat(),
        "session": [s.name for s in schedule.active_sessions(now)] if schedule.confirmed else [],
        "schedule_confirmed": schedule.confirmed,
        "next_wake_utc": nxt[0].isoformat() if nxt else None,
        "sleep_blocked": inhibitor.active,
        "network": STATE["failure"],
        "internet": STATE["internet"],
        "recovery_enabled": STATE["recovery_enabled"],
        "entries_suspended": STATE["entries_suspended"],
        "last_health_ok": STATE["last_health_ok"],
        "accounts": db.accounts(),
    }


@app.get("/status")
def status(_: str = Depends(auth)):
    return status_payload()


@app.get("/events")
def events(_: str = Depends(auth)):
    return db.recent_events(100)


class Toggle(BaseModel):
    value: bool


@app.post("/control/recovery")
def toggle_recovery(body: Toggle, who: str = Depends(auth)):
    STATE["recovery_enabled"] = body.value
    db.audit(who, "recovery_enabled", str(body.value))
    return {"recovery_enabled": body.value}


@app.post("/control/suspend-entries")
def suspend_entries(body: Toggle, who: str = Depends(auth)):
    STATE["entries_suspended"] = body.value
    db.audit(who, "entries_suspended", str(body.value))
    return {"entries_suspended": body.value}


@app.post("/control/accounts/{account_id}/enable")
def enable_account(account_id: int, body: Toggle, who: str = Depends(auth)):
    db.set_account_enabled(account_id, body.value)
    db.audit(who, "account_enabled", f"{account_id}={body.value}")
    return {"ok": True}


@app.websocket("/ws")
async def ws(socket: WebSocket, token: str = ""):
    if not token or not db.device_valid(hash_token(token)):
        await socket.close(code=4401)
        return
    await socket.accept()
    sockets.add(socket)
    try:
        while True:
            await socket.send_json(status_payload())
            await asyncio.sleep(5)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        sockets.discard(socket)


# ---------------- ops agent loop ----------------
def probes() -> Probes:
    return Probes(
        adapter_up=lambda: True,  # refined on Windows via netsh in a later iteration
        router_reachable=lambda: True,
        internet_reachable=lambda: tcp_probe("1.1.1.1", 443)[0],
        dns_resolves=dns_probe,
        broker_reachable=lambda: True,  # per-account broker host check added with MT5 workers
        api_reachable=lambda: True,
        tailscale_connected=tailscale_probe,
        mt5_connected=lambda: None,
    )


def ops_loop() -> None:
    last_session = None
    while True:
        try:
            failure = classify(probes())
            STATE["failure"], STATE["internet"] = failure.value, failure not in (
                Failure.INTERNET_DOWN, Failure.ADAPTER_DOWN, Failure.ROUTER_DOWN)
            if failure is Failure.NONE:
                STATE["last_health_ok"] = datetime.now(timezone.utc).isoformat()
            in_session = schedule.confirmed and schedule.is_trading_time(datetime.now(timezone.utc))
            if in_session and not inhibitor.active:
                inhibitor.start()
                db.event("session_started", "sleep blocked for trading session")
            elif not in_session and inhibitor.active:
                inhibitor.stop()
                db.event("session_ended", "sleep block released; open positions are untouched")
            if schedule.confirmed and sys.platform == "win32":
                nxt = schedule.next_wake(datetime.now(timezone.utc))
                if nxt and STATE.get("wake_for") != nxt[0].isoformat():
                    exe = sys.executable if getattr(sys, "frozen", False) else sys.executable
                    power.schedule_wake(nxt[0], exe)
                    STATE["wake_for"] = nxt[0].isoformat()
                    db.event("wake_scheduled", f"PC will wake at {schedule.to_local(nxt[0]):%Y-%m-%d %H:%M} New York time")
        except Exception as exc:  # noqa: BLE001 - the agent must never die
            db.event("ops_error", f"{type(exc).__name__}: {exc}"[:300])
        time.sleep(30)


def main() -> None:
    threading.Thread(target=ops_loop, daemon=True, name="ops-loop").start()
    host = os.environ.get("TRADINGOPS_HOST", "0.0.0.0")
    port = int(os.environ.get("TRADINGOPS_PORT", "8765"))
    print(f"TradingOps running. Open http://127.0.0.1:{port} on this PC.")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
