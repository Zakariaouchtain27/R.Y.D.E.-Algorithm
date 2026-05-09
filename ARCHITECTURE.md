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
     │                   On next cycle: re-evaluate and STRIKE or release.
     │
     └── STRIKE       → High confidence. Cancel + rebook immediately.

  4. Notify passenger via webhook
```

## Engine: Key Fixes vs v1

| v1 Flaw | v2 Fix |
|---|---|
| Monotone sigmoid — far-out savings always suppressed to 0 | U-shaped pricing curve: valley at ~42d, near-zero at <7d, high at >90d |
| `volatility_index` silently defaulted to 1.0 everywhere | Passed through and used to reshape the curve |
| Naive `savings / historical_max` ratio | Expected-value model: `strike_value = net_savings - E[future_gain]` |
| No seat inventory awareness | `seat_urgency_multiplier` amplifies score exponentially as seats deplete |
| `safe_max_drop` hack inflated score on new routes | Conservative 30% estimate when no history; no artificial 1.0 clamp |

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
| Store | SQLite | PostgreSQL + SQLAlchemy |
| Hold state | In-memory dict | Redis hash with TTL |
| Notifications | HTTP webhook | SQS/Pub-Sub + SendGrid/Twilio |
| Multi-instance | Single process | Kubernetes + leader election |

## Running the Demo

```bash
pip install -r requirements.txt
python demo.py
```

## Running the Bot

```bash
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your API keys
python main.py
```
