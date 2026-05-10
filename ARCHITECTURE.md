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
│  │ PRISMEngine  │ │PhantomHold   │ │BookingStore  │       │
│  │ (LSMC / MC)  │ │  Manager     │ │  (SQLite)    │       │
│  └──────┬───────┘ └──────────────┘ └──────────────┘       │
│         │                                                   │
│  ┌──────┴────────────────────────────────────────┐        │
│  │              PRISM Sub-modules                │        │
│  │  OrnsteinUhlenbeck  │  LSMCOptimalStopper     │        │
│  │  LoadFactorPressure │  CompetitiveCascade     │        │
│  │  PriceHistory (SQLite rolling median)         │        │
│  └───────────────────────────────────────────────┘        │
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
  2. Feed into PRISMEngine (Longstaff-Schwartz Monte Carlo)
     a. Fit Ornstein-Uhlenbeck process (κ, σ, θ) on price history
     b. Simulate 5,000 price paths over remaining days
     c. LSMC backward induction → expected continuation value
     d. ratio = intrinsic_savings / LSMC_expected_value
  3. Apply override signals:
     │
     ├── Load factor pressure > 78%  → PHANTOM_HOLD (airline likely to cut)
     └── Competitive cascade > 72%   → PHANTOM_HOLD (market moving)
  4. Engine outputs:
     │
     ├── IGNORE       → net_savings ≤ 0 after fees. Do nothing.
     │
     ├── WAIT         → ratio < 0.70. Better price statistically probable.
     │
     ├── PHANTOM_HOLD → ratio 0.70–0.95. Create 24h fare lock.
     │                   On next cycle: re-evaluate — STRIKE or keep/release.
     │
     └── STRIKE       → ratio ≥ 0.95. Cancel + rebook immediately.

  5. Notify passenger via webhook
```

## Phantom Hold Cycle

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

## PRISM Engine — Algorithm Detail

| Step | Component | What it does |
|---|---|---|
| 1 | `OrnsteinUhlenbeck.fit()` | OLS regression on ΔP to estimate κ (mean-reversion), σ (volatility), θ (rolling median reference) |
| 2 | `OrnsteinUhlenbeck.simulate_paths()` | 5,000 Monte Carlo paths with U-curve seasonality: Gaussian dip at ~42d, exponential spike <7d |
| 3 | `LSMCOptimalStopper.compute()` | Longstaff-Schwartz backward induction with polynomial basis [1, z, z², z³]; returns E[optimal stopping value] |
| 4 | `LoadFactorPressureModel.pressure()` | EMSR-based sigmoid on shortfall vs 85% load-factor target; configurable `total_seats` |
| 5 | `CompetitiveCascadeDetector.cascade_probability()` | Log-normal cascade delay model; P(cascade) = 1 − (1 − BASE·CDF)^n |
| 6 | `PRISMEngine.evaluate()` | Combines LSMC ratio + overrides → `RYDEDecision` with real MC probability_of_future_drop |

## Bug Fixes — v2.1 → PRISM

| # | Bug | File | Impact | Fix |
|---|---|---|---|---|
| 1 | **End of Time trap** | `engine.py` | Bot outputs WAIT when flight is tomorrow and savings exist — plane takes off, savings lost | When `prob_drop < 5%` and `net_savings > 0`, force `ceiling = net_savings` so ratio → 1.0 and STRIKE fires |
| 2 | **Premature Execution** | `bot.py` | 24h Phantom Hold silently became a 1h hold — bot rebooked on next poll | `_handle_hold_cycle` only executes on STRIKE; PHANTOM_HOLD does nothing; WAIT/IGNORE releases |
| 3 | **API Phantom Route** | `duffel.py` | Every cancellation returned 404 | 2-step Duffel flow: `POST /order_cancellations` → `POST /order_cancellations/{id}/actions/confirm` |
| 4 | **U-curve discontinuity** | `stochastic.py` | 12% price jump at d=7 boundary → biased MC paths | Continuous formula: last-minute ramp anchored to d=7 main-curve value |
| 5 | **Hardcoded 180 seats** | `competitive.py` | Wrong pressure for widebody/regional aircraft | `total_seats` is now a parameter (default 180) |
| 6 | **All-time median anchor** | `price_history.py` | Reference price anchored to stale history on trending routes | Rolling last-30-snapshot median |
| 7 | **Fake drop probability** | `engine.py` | `probability_of_future_drop` was `(1-ratio)*100`, not a real probability | Real MC evidence: fraction of 5,000 paths whose minimum falls below current price |
| 8 | **Startup crash** | `main.py` / `run_all.py` | `TypeError` on every startup — `strike_threshold` / `phantom_hold_threshold` no longer accepted by PRISMEngine | Removed legacy kwargs; PRISM uses dynamic Monte Carlo, not static thresholds |

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

> **SQLite note:** SQLite with thread locks works perfectly for a single-server MVP.
> In production, concurrent web requests would throttle on SQLite's write lock.
> The store layer (`store.py`, `price_history.py`) is intentionally isolated behind
> a minimal interface so swapping to PostgreSQL via SQLAlchemy only requires
> rewriting those two files — zero changes to the PRISM engine, bot, or adapters.

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
