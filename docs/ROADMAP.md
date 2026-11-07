# Roadmap and status

Updated: 2026-11-07. Environment: Linux sandbox, no Windows, no MT5, no Flutter SDK, no network installs.

## What this environment can and cannot do

| Deliverable | Status |
|---|---|
| Python source for scheduler, ops agent, risk | Written and unit-tested here |
| Windows `.exe` | Cannot be built here (needs Windows + PyInstaller). Build script planned in `/build` |
| Android `.apk` | Cannot be built here (needs Flutter + Android SDK). Source planned in `/mobile` |
| Live MT5 connection and demo-account tests | Need Windows + MetaTrader5 package + a broker demo account. Not testable here |

No part of the project is complete until the EXE and APK are built and the Windows-only stages are run on your PC.

## Phase 1 (done): foundations, tested on Linux

- `strategy_engine/sessions.py`: IANA timezone sessions, overlap/typo flags, confirmation gate, wake times (9 tests)
- `ops_agent/power.py`: sleep inhibitor, wake-task builder, powercfg parsing, boot classification, Wake-on-LAN (tests for logic; Windows calls are guarded)
- `ops_agent/network_monitor.py`: layered failure classification, outage tracking, retry limits
- `ops_agent/recovery.py`: whitelisted actions, forbidden list, 7-step guarded restart, no MT5 restart during network outage
- `risk_manager/risk.py`: round-down sizing, caps, SL/TP checks, account limits, rejection reasons

39 tests pass.

## Phase 2 (next): needs your decisions and a Windows machine

1. `database` (SQLite schema, audit log, idempotency keys)
2. `security` (DPAPI credential vault, device pairing, tokens)
3. `connection_manager` (MetaTrader5 login, one isolated terminal per account)
4. `execution_engine` (order flow, broker-confirmed results, reconciliation)
5. `backend` (FastAPI + WebSocket, authenticated)
6. Ops agent runtime: Windows service, heartbeat, daily report, health score

## Phase 3: apps and packaging

7. Flutter Android app (black / neon blue), notifications, remote controls
8. Bot importer (MQL5, Python sandboxed, Pine translation workflow)
9. PyInstaller + installer build; `flutter build apk`
10. Stages 2-10 of the test pipeline on real demo accounts

## Open decisions

- Trading days per session (currently Mon-Fri; forex also opens Sunday evening)
- Is 07:00-20:00 intended, or 07:00-10:00? It contains the 12:00-15:00 session
- Wi-Fi or Ethernet (Wake-on-LAN needs Ethernet)
- PC type: desktop or laptop (Modern Standby affects wake reliability)
- Which Android notification route: FCM push, or ntfy over Tailscale
