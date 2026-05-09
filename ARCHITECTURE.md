# R.Y.D.E. v2 — System Architecture

## Overview

```
┌─────────────────────────────────────────────────────────────┐
│                        RYDE Bot                             │
│                                                             │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │ PriceMonitor │───▶│   RYDEBot    │───▶│  Notifier    │  │
│  │ (APScheduler)│    │ (Orchestrator│    │ (Webhooks)   │  │
│  └──────────────┘    └──────┬───────┘    └──────────────┘  │
│                             │                               │
│              ┌──────────────┼──────────────┐               │
│              ▼              ▼              ▼               │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐       │
│  │    Engine    │ │PhantomHold   │ │BookingStore  │       │
│  │ (Algorithm)  │ │  Manager     │ │  (SQLite)    │       │
│  └──────────────┘ └──────────────┘ └──────────────┘       │
│                             │                               │
│              ┌──────────────┼──────────────┐               │
│              ▼                             ▼               │
│  ┌─────────────────────┐   ┌──────────────────────────┐   │
│  │   DuffelAdapter     │   │    AmadeusAdapter        │   │
│  │ (search+hold+book)  │   │  (price monitoring only) │   │
│  └─────────────────────┘   └──────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
  ┌─────────────┐               ┌─────────────┐
  │  Duffel API │               │ Amadeus API │
  │ (300+ NDC   │               │ (IATA GDS   │
  │  airlines)  │               │  search)    │
  └─────────────┘               └─────────────┘
```

## Decision Flow

```
For each booking every POLL_INTERVAL_MINUTES:

  1. Fetch current best price via adapter
  2. Feed into RegretMinimizationEngine
  3. Engine outputs:
     │
     ├── IGNORE       → net_savings ≤ 0 after fees. Do nothing.
     │
     ├── WAIT         → E[future_gain] > strike_value. Keep watching.
     │
     ├── PHANTOM_HOLD → Moderate confidence. Create 24h fare lock.
     │                   On next cycle: re-evaluate — STRIKE or keep holding or release.
     │
     └── STRIKE       → High confidence. Cancel + rebook immediately.

  4. Notify passenger via webhook
```

## Phantom Hold Cycle (corrected behaviour)

```
Hold created (24h TTL)
       │
       │  every 60 min poll
       ▼
  Re-evaluate price
       │
       ├── STRIKE       → Price dropped further / time running out → REBOOK NOW
       ├── PHANTOM_HOLD → Market stable, savings still valid → DO NOTHING, keep holding
       └── WAIT/IGNORE  → Price moved back up, savings gone → RELEASE HOLD, resume monitoring
```

## Engine: Key Fixes vs v1

| v1 Flaw | v2 Fix |
|---|---|
| Monotone sigmoid — far-out savings always suppressed to 0 | U-shaped pricing curve: valley at ~42d, near-zero at <7d, high at >90d |
| `volatility_index` silently defaulted to 1.0 everywhere | Passed through and used to reshape the curve |
| Naive `savings / historical_max` ratio | Expected-value model: `strike_value = net_savings - E[future_gain]` |
| No seat inventory awareness | `seat_urgency_multiplier` amplifies score exponentially as seats deplete |
| `safe_max_drop` hack inflated score on new routes | Conservative 30% estimate when no history; no artificial 1.0 clamp |

## Critical Bug Fixes (v2.1)

| # | Bug | File | Impact | Fix |
|---|---|---|---|---|
| 1 | **End of Time trap** | `engine.py` | Bot outputs WAIT when flight is tomorrow and savings exist — plane takes off, savings lost | When `prob_drop < 5%` and `net_savings > 0`, force `ceiling = net_savings` so score normalizes to 1.0 and STRIKE fires |
| 2 | **Premature Execution** | `bot.py` | 24h Phantom Hold silently became a 1h hold — bot rebooked on the very next poll if engine still returned PHANTOM_HOLD | `_handle_hold_cycle` now only executes on STRIKE; PHANTOM_HOLD does nothing; WAIT/IGNORE releases the hold |
| 3 | **API Phantom Route** | `duffel.py` | Every cancellation returned 404 — zero real rebooks would ever succeed in production | Replaced single-step cancel with Duffel’s required 2-step flow: `POST /order_cancellations` → `POST /order_cancellations/{id}/actions/confirm` |

## Third-Party Integration Strategy

### Legal Access Paths (no ToS violations)

| Method | Capability | Requires |
|---|---|---|
| **Duffel API** | Full lifecycle: search, hold, book, cancel | Account + balance top-up |
| **Amadeus for Developers** | Price monitoring only | Free registration |
| **Sabre / Travelport** | Full GDS access | Travel agency agreement (IATA/ARC) |
| **Airline NDC direct** | Per-airline (BA, AA, LH, etc.) | Per-airline developer program |
| **OTA affiliate APIs** | Booking.com, Expedia partner APIs | Partner agreement |

### Scaling to Production

| Component | Dev (current) | Production swap |
|---|---|---|
| Scheduler | APScheduler in-process | Celery Beat + Redis |
| Store | SQLite + thread lock | PostgreSQL + SQLAlchemy |
| Hold state | In-memory dict | Redis hash with TTL |
| Notifications | HTTP webhook | SQS/Pub-Sub + SendGrid/Twilio |
| Multi-instance | Single process | Kubernetes + leader election |

> **SQLite note (for interviews):** SQLite with thread locks works perfectly for a
> single-server MVP. In production, concurrent web requests would throttle on
> SQLite’s write lock. The store layer (`store.py`, `client_store.py`) is
> intentionally isolated behind a minimal interface so swapping to PostgreSQL
> via SQLAlchemy only requires rewriting those two files — zero changes to the
> engine, bot, or adapters.

## Running the Demo

```bash
pip install -r requirements.txt
python demo.py
```

## Running Everything (bot + web)

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
python3 run_all.py
# Open http://localhost:8000
```
