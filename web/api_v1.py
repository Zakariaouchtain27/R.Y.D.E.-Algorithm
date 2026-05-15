"""
RYDE B2B API v1
===============
All endpoints require:  X-Agency-Key: <agency_key>

How PRISM evaluation works
--------------------------
PRISM (Longstaff-Schwartz Monte Carlo) runs automatically whenever a
current_price is provided:

  POST /monitor           — include current_price to evaluate on submit
  PATCH /bookings/{id}    — push a new current_price to re-evaluate

Duplicate-webhook protection
-----------------------------
Each PRISM run records last_evaluated_price + last_evaluated_at in the
booking metadata. A subsequent PATCH with the same price is silently
skipped unless at least 5 minutes have passed (price unchanged but time
decay could shift the decision). Different price always re-evaluates.

Webhook security
-----------------
Every webhook POST is signed:
  X-RYDE-Signature: sha256=<hmac-sha256-hex>
Verify with: hmac.new(RYDE_WEBHOOK_SECRET, body, sha256).hexdigest()

Time-decay scan frequency
--------------------------
Background scheduler runs every 15 minutes.
Per booking, re-evaluation frequency scales with urgency:
  >= 14 days to departure  →  re-evaluate every 60 minutes
   7–14 days to departure  →  re-evaluate every 30 minutes
    < 7 days to departure  →  re-evaluate every 15 minutes

Idempotency
-----------
POST /monitor accepts an optional  Idempotency-Key: <uuid>  header.
Returns the original 201 response on duplicate keys — safe to retry.

Success-fee billing
-------------------
Every STRIKE decision triggers an automatic 20% success-fee charge
against the agency's stored Stripe card (stripe_customer_id in agencies
table). Requires STRIPE_SECRET_KEY env var and a Stripe customer on file.
All charge outcomes are written to the audit trail:
  billing_charged — fee_usd, net_savings, stripe_payment_intent
  billing_error   — fee_usd, error; also fires ryde.billing_error webhook
  billing_skipped — no_stripe_customer (card not yet on file)

Endpoints
---------
POST   /api/v1/predict              Pre-booking: BOOK or WAIT decision
POST   /api/v1/monitor              Submit a booking for PRISM monitoring
GET    /api/v1/bookings             List all bookings for your agency
GET    /api/v1/bookings/{id}        Get a single booking + status
GET    /api/v1/bookings/{id}/audit  Immutable decision + lifecycle trail
PATCH  /api/v1/bookings/{id}        Push new price / update booking details
DELETE /api/v1/bookings/{id}        Stop monitoring a booking
GET    /api/v1/analytics            Agency-level savings + usage stats
GET    /api/v1/account              Your agency profile and quota info
"""
import asyncio
import gc
import logging
import os
import time
import uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import List, Literal, Optional

import numpy as np
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field, field_validator, model_validator

from ryde.agency_store import AgencyStore
from ryde.models import Booking, Passenger, PriceSnapshot, RYDEAction
from ryde.notifier import Notifier
from ryde.prism import PRISMEngine
from ryde.store import BookingStore

router = APIRouter(prefix="/api/v1", tags=["B2B API v1"])
log    = logging.getLogger(__name__)

_db_path  = os.getenv("RYDE_DB_PATH", "ryde.db")
_bookings = BookingStore(_db_path)
_agencies = AgencyStore(_db_path)
_engine   = PRISMEngine(_db_path)
_notifier = Notifier()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prism")

# ---------------------------------------------------------------------------
# Stripe success-fee client (None when STRIPE_SECRET_KEY is not configured)
# ---------------------------------------------------------------------------

def _make_stripe_client():
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        return None
    try:
        from .stripe_client import StripeClient
        return StripeClient(key)
    except ImportError:
        log.warning("stripe package not installed — success-fee billing disabled")
        return None


_stripe_client = _make_stripe_client()

# Seconds between re-evaluations per booking based on days to departure.
_EVAL_INTERVAL = {"far": 3600, "close": 1800, "urgent": 900}
_PATCH_COOLDOWN = 300


def _scan_interval(days: int) -> int:
    if days >= 14:
        return _EVAL_INTERVAL["far"]
    if days >= 7:
        return _EVAL_INTERVAL["close"]
    return _EVAL_INTERVAL["urgent"]


# ---------------------------------------------------------------------------
# Rate limiting  (60 requests / 60 seconds per key, in-memory)
# ---------------------------------------------------------------------------

_call_log: dict = defaultdict(list)
_RATE_LIMIT  = 60
_RATE_WINDOW = 60


def _check_rate(api_key: str) -> None:
    now = time.monotonic()
    _call_log[api_key] = [t for t in _call_log[api_key] if now - t < _RATE_WINDOW]
    if len(_call_log[api_key]) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit: {_RATE_LIMIT} requests / {_RATE_WINDOW}s.",
            headers={"Retry-After": "60"},
        )
    _call_log[api_key].append(now)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def require_api_key(
    x_agency_key: Optional[str] = Header(default=None),
    x_api_key:    Optional[str] = Header(default=None),
) -> tuple:
    key = x_agency_key or x_api_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Agency-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )
    agency = _agencies.get_by_key(key)
    if not agency:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or revoked API key.",
        )
    _check_rate(key)
    _agencies.log_call(key)
    return agency.name, key


# ---------------------------------------------------------------------------
# PRISM evaluation helpers
# ---------------------------------------------------------------------------

def _seconds_since(iso_str: str) -> float:
    try:
        ts = datetime.fromisoformat(iso_str.rstrip("Z")).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds()
    except Exception:
        return float("inf")


def _charge_success_fee_sync(
    booking: Booking,
    agency_name: str,
    net_savings: float,
) -> None:
    """
    Charge 20% success fee against the agency's Stripe card on file.

    Runs synchronously inside the PRISM executor thread.
    Writes billing_charged / billing_error / billing_skipped to audit trail.
    """
    if _stripe_client is None:
        log.debug(
            "STRIPE_SECRET_KEY not configured — skipping success fee for %s",
            booking.booking_id,
        )
        return

    fee     = round(net_savings * 0.20, 2)
    now_iso = datetime.utcnow().isoformat() + "Z"

    ag = _agencies.get_by_name(agency_name)
    if not ag or not ag.stripe_customer_id:
        log.warning(
            "No Stripe customer on file for agency '%s' — cannot charge success fee for %s",
            agency_name, booking.booking_id,
        )
        _bookings.log_audit(booking.booking_id, agency_name, "billing_skipped", {
            "reason":      "no_stripe_customer",
            "fee_usd":     fee,
            "net_savings": round(net_savings, 2),
            "skipped_at":  now_iso,
        })
        return

    success, pi, error = _stripe_client.charge_success_fee(
        customer_id=ag.stripe_customer_id,
        amount_usd=fee,
        booking_id=booking.booking_id,
        net_savings=net_savings,
    )

    if success:
        pi_id = pi.id if pi else None
        log.info(
            "Success fee $%.2f charged for booking %s (agency: %s, pi: %s)",
            fee, booking.booking_id, agency_name, pi_id,
        )
        _bookings.log_audit(booking.booking_id, agency_name, "billing_charged", {
            "fee_usd":               fee,
            "net_savings":           round(net_savings, 2),
            "fee_pct":               20,
            "stripe_payment_intent": pi_id,
            "charged_at":            now_iso,
        })
    else:
        log.error(
            "Success fee charge FAILED for booking %s (agency: %s): %s",
            booking.booking_id, agency_name, error,
        )
        _bookings.log_audit(booking.booking_id, agency_name, "billing_error", {
            "fee_usd":    fee,
            "net_savings": round(net_savings, 2),
            "error":      error or "Unknown error",
            "failed_at":  now_iso,
        })
        _notifier.billing_error(booking, fee, error or "Unknown error")


def _run_prism_sync(
    booking_id: str,
    current_price: float,
    seats_remaining: int,
    agency: str,
) -> str:
    """
    CPU-bound: runs 5,000 Monte Carlo paths. Always called via run_in_executor.
    """
    booking = _bookings.get_by_id(booking_id)
    if not booking:
        log.error("PRISM: booking %s not found", booking_id)
        return "error"

    snapshot = PriceSnapshot(
        booking_id=booking_id,
        current_price=current_price,
        seats_remaining=seats_remaining,
        snapshot_time=datetime.utcnow(),
        fare_id="agency-provided",
        source="b2b_api",
    )
    try:
        decision = _engine.evaluate(booking, snapshot)
    except Exception as exc:
        log.error("PRISM evaluation failed [%s]: %s", booking_id, exc)
        return "error"

    log.info(
        "PRISM [%s] → %s (score=%.1f, net_savings=$%.2f)",
        booking_id, decision.action.value,
        decision.confidence_score, decision.net_savings,
    )

    now_iso = datetime.utcnow().isoformat() + "Z"

    _bookings.log_audit(booking_id, agency, "decision", {
        "action":           decision.action.value,
        "confidence_score": decision.confidence_score,
        "net_savings":      round(decision.net_savings, 2),
        "current_price":    current_price,
        "seats_remaining":  seats_remaining,
        "reasoning":        decision.reasoning,
        "evaluated_at":     now_iso,
    })

    booking.metadata["last_evaluated_price"] = current_price
    booking.metadata["last_evaluated_at"]    = now_iso
    _bookings.upsert(booking)

    if decision.action in (RYDEAction.STRIKE, RYDEAction.PHANTOM_HOLD):
        _notifier.decision(booking, decision)

    # Charge 20% success fee on every STRIKE.
    # Signal API mode: agency acts on the webhook; we bill at decision time.
    if decision.action == RYDEAction.STRIKE:
        net_savings = booking.original_price - current_price - booking.cancellation_fee
        if net_savings > 0:
            _charge_success_fee_sync(booking, agency, net_savings)

    return decision.action.value


async def _trigger_prism(
    booking_id: str,
    current_price: float,
    seats_remaining: int,
    agency: str,
) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        _executor,
        _run_prism_sync,
        booking_id, current_price, seats_remaining, agency,
    )


async def scan_all_active() -> int:
    """
    Re-evaluate active B2B bookings using stored current_price.
    Called by the 15-minute background task in app.py.
    """
    rows = _bookings.get_active()
    b2b  = [b for b in rows if b.metadata.get("source") == "b2b_api_v1"]
    if not b2b:
        return 0

    log.info("PRISM scan: %d active B2B booking(s) to check", len(b2b))
    count = 0
    now   = datetime.utcnow()

    for booking in b2b:
        current_price = float(booking.metadata.get("current_price") or 0)
        if current_price <= 0:
            continue

        days   = max(0, (booking.departure_date.replace(tzinfo=None) - now).days)
        needed = _scan_interval(days)

        last_eval = booking.metadata.get("last_evaluated_at", "")
        if last_eval and _seconds_since(last_eval) < needed:
            continue

        seats  = int(booking.metadata.get("seats_remaining") or 9)
        agency = booking.metadata.get("agency", "unknown")
        try:
            await _trigger_prism(booking.booking_id, current_price, seats, agency)
            count += 1
        except Exception as exc:
            log.error("Scan failed [%s]: %s", booking.booking_id, exc)

    if count:
        log.info("PRISM scan complete: %d evaluated", count)
    return count


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _booking_to_response(d: dict) -> dict:
    meta = d.get("metadata", {})
    return {
        "tracking_id":      d["booking_id"],
        "route":            f"{d['origin']}-{d['destination']}",
        "origin":           d["origin"],
        "destination":      d["destination"],
        "departure_date":   d["departure_date"][:10],
        "original_price":   d["original_price"],
        "current_price":    meta.get("current_price"),
        "cancellation_fee": d["cancellation_fee"],
        "currency":         d["currency"],
        "cabin_class":      d.get("cabin_class", "economy"),
        "fare_type":        meta.get("fare_type", "refundable"),
        "status":           "monitoring" if d.get("_active", True) else "stopped",
        "webhook_url":      d.get("notify_webhook"),
        "passenger_set":    d["passenger"]["given_name"] != "Pending",
        "last_decision":    meta.get("last_evaluated_at"),
        "submitted_at":     d.get("_created_at"),
        "updated_at":       d.get("_updated_at"),
        "metadata":         meta,
    }


# ---------------------------------------------------------------------------
# POST /predict
# ---------------------------------------------------------------------------

class PredictRequest(BaseModel):
    origin:          str   = Field(..., min_length=3, max_length=3)
    destination:     str   = Field(..., min_length=3, max_length=3)
    departure_date:  str   = Field(..., description="YYYY-MM-DD")
    current_price:   float = Field(..., gt=0)
    seats_remaining: int   = Field(9, ge=0)

    @field_validator("origin", "destination")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("departure_date")
    @classmethod
    def _future(cls, v: str) -> str:
        try:
            dep = datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("departure_date must be YYYY-MM-DD")
        if dep.date() <= datetime.now().date():
            raise ValueError("departure_date must be in the future")
        return v


def _run_predict_sync(
    origin: str,
    destination: str,
    departure_date: str,
    current_price: float,
    seats_remaining: int,
) -> dict:
    from ryde.prism.price_history import make_route_key
    from ryde.prism.stochastic import OrnsteinUhlenbeck

    N_PATHS             = 5000
    WAIT_PROB_THRESHOLD = 0.55
    WAIT_DROP_THRESHOLD = 10.0

    dep     = datetime.strptime(departure_date, "%Y-%m-%d")
    days    = max(1, (dep.replace(tzinfo=None) - datetime.now().replace(tzinfo=None)).days)
    horizon = min(14, days)

    route_key       = make_route_key(origin, destination, departure_date)
    ou              = OrnsteinUhlenbeck()
    price_series    = _engine.history.get_price_series(route_key)
    if len(price_series) >= 10:
        ou.fit(price_series)

    reference_price = _engine.history.get_reference_price(route_key) or current_price
    theta_now       = ou.u_curve_mean(reference_price, days)

    rng   = np.random.default_rng()
    paths = ou.simulate_paths(
        current_price=current_price,
        reference_price=reference_price,
        days=horizon,
        n_paths=N_PATHS,
        rng=rng,
    )

    min_prices         = paths.min(axis=1)
    expected_end_price = float(paths[:, -1].mean())
    will_drop          = min_prices < current_price
    prob_drop          = float(will_drop.mean())

    if will_drop.sum() > 0:
        drop_amounts  = current_price - min_prices[will_drop]
        expected_drop = float(drop_amounts.mean())
        drop_p95      = float(np.percentile(drop_amounts, 95))
    else:
        expected_drop = drop_p95 = 0.0

    pct_above = (current_price - theta_now) / theta_now

    if prob_drop > WAIT_PROB_THRESHOLD and expected_drop > WAIT_DROP_THRESHOLD:
        decision   = "WAIT"
        confidence = round(min(prob_drop * 100, 99.0), 1)
    else:
        decision   = "BOOK"
        confidence = round(min((1.0 - prob_drop) * 100, 99.0), 1)

    pct_pct = abs(pct_above) * 100
    if pct_above > 0.005:
        position = f"Price is currently {pct_pct:.1f}% above the predicted mean-reversion level of ${theta_now:.0f}"
    elif pct_above < -0.005:
        position = f"Price is currently {pct_pct:.1f}% below the predicted fair-value level of ${theta_now:.0f} — the fare is already attractively priced"
    else:
        position = f"Price is trading at its predicted fair-value level of ${theta_now:.0f}"

    if decision == "WAIT":
        reasoning = (
            f"{position}. Monte Carlo analysis of {N_PATHS:,} simulated OU price paths over the "
            f"next {horizon} days shows a {prob_drop * 100:.0f}% probability of a cheaper fare "
            f"emerging, with an expected saving of ${expected_drop:.0f} (95th-percentile: ${drop_p95:.0f}). "
            f"The model projects the fare will reach ${expected_end_price:.0f} by day {horizon}. "
            f"Statistical evidence favours waiting."
        )
    elif pct_above < -0.005:
        reasoning = (
            f"{position}. With only a {prob_drop * 100:.0f}% probability of further decline "
            f"(expected drop: ${expected_drop:.0f}), the risk of waiting outweighs the opportunity cost. "
            f"Booking now locks in a fare already below model fair value."
        )
    else:
        reasoning = (
            f"{position}. Simulation of {N_PATHS:,} OU paths over {horizon} days shows a "
            f"{prob_drop * 100:.0f}% probability of a price drop averaging ${expected_drop:.0f}. "
            f"The projected fare in {horizon} days is ${expected_end_price:.0f}. "
            f"Drop probability is below the WAIT threshold — booking now is the lower-risk choice."
        )

    del paths
    gc.collect()

    return {
        "decision":                  decision,
        "confidence_score":          confidence,
        "expected_future_drop":      round(expected_drop, 2),
        "prob_drop_pct":             round(prob_drop * 100, 1),
        "expected_price_in_horizon": round(expected_end_price, 2),
        "mean_reversion_target":     round(theta_now, 2),
        "prediction_horizon_days":   horizon,
        "reasoning":                 reasoning,
        "model": {
            "kappa":                  round(ou.kappa, 4),
            "sigma":                  round(ou.sigma, 2),
            "paths_simulated":        N_PATHS,
            "historical_data_points": len(price_series),
            "reference_price":        round(reference_price, 2),
        },
    }


@router.post("/predict", summary="Pre-booking price prediction")
async def predict_price(
    payload: PredictRequest,
    auth: tuple = Depends(require_api_key),
):
    loop   = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        _executor,
        _run_predict_sync,
        payload.origin,
        payload.destination,
        payload.departure_date,
        payload.current_price,
        payload.seats_remaining,
    )
    log.info(
        "PRISM predict [%s-%s %s $%.0f] → %s (P(drop)=%.0f%%, E[drop]=$%.0f)",
        payload.origin, payload.destination, payload.departure_date,
        payload.current_price, result["decision"],
        result["prob_drop_pct"], result["expected_future_drop"],
    )
    return {
        "route":          f"{payload.origin}-{payload.destination}",
        "origin":         payload.origin,
        "destination":    payload.destination,
        "departure_date": payload.departure_date,
        "current_price":  payload.current_price,
        "predicted_at":   datetime.utcnow().isoformat() + "Z",
        **result,
    }


# ---------------------------------------------------------------------------
# POST /monitor
# ---------------------------------------------------------------------------

class MonitorRequest(BaseModel):
    origin:           str   = Field(..., min_length=3, max_length=3)
    destination:      str   = Field(..., min_length=3, max_length=3)
    departure_date:   str   = Field(..., description="YYYY-MM-DD")
    original_price:   float = Field(..., gt=0)
    cancellation_fee: float = Field(..., ge=0)
    current_price:    Optional[float] = Field(None, gt=0)
    seats_remaining:  int   = Field(9, ge=0)
    cabin_class:      Literal["economy", "premium_economy", "business", "first"] = "economy"
    fare_type:        Literal["refundable", "partially_refundable"] = "refundable"
    webhook_url:  Optional[str] = None
    reference:    Optional[str] = None

    @field_validator("origin", "destination")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper().strip()

    @field_validator("departure_date")
    @classmethod
    def _future(cls, v: str) -> str:
        try:
            dep = datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("departure_date must be YYYY-MM-DD")
        if dep.date() <= datetime.now().date():
            raise ValueError("departure_date must be in the future")
        return v

    @model_validator(mode="after")
    def _economics_check(self) -> "MonitorRequest":
        if self.cancellation_fee >= self.original_price:
            raise ValueError(
                f"cancellation_fee ({self.cancellation_fee}) must be less than "
                f"original_price ({self.original_price})."
            )
        return self


@router.post("/monitor", status_code=201)
async def submit_monitor(
    payload: MonitorRequest,
    auth: tuple = Depends(require_api_key),
    idempotency_key: Optional[str] = Header(default=None),
):
    agency, _ = auth

    if idempotency_key:
        cached = _bookings.get_idempotency(idempotency_key)
        if cached:
            return cached["response"]

    tracking_id = f"b2b_{uuid.uuid4().hex[:16]}"
    now_iso     = datetime.utcnow().isoformat() + "Z"

    meta: dict = {
        "source":          "b2b_api_v1",
        "agency":          agency,
        "fare_type":       payload.fare_type,
        "submitted_at":    now_iso,
        "seats_remaining": payload.seats_remaining,
    }
    if payload.current_price is not None:
        meta["current_price"] = payload.current_price

    booking = Booking(
        booking_id=tracking_id,
        passenger=Passenger(
            title="mr", given_name="Pending", family_name="Pending",
            born_on="1990-01-01", gender="m",
            email=f"noreply+{tracking_id}@b2b.ryde.invalid",
            phone="+10000000000",
        ),
        origin=payload.origin,
        destination=payload.destination,
        departure_date=datetime.strptime(payload.departure_date, "%Y-%m-%d"),
        original_price=payload.original_price,
        currency="USD",
        cancellation_fee=payload.cancellation_fee,
        cabin_class=payload.cabin_class,
        adapter="b2b",
        adapter_booking_ref=payload.reference or tracking_id,
        notify_webhook=payload.webhook_url,
        metadata=meta,
    )
    _bookings.upsert(booking)

    _bookings.log_audit(tracking_id, agency, "submitted", {
        "route":            f"{payload.origin}-{payload.destination}",
        "original_price":   payload.original_price,
        "current_price":    payload.current_price,
        "cancellation_fee": payload.cancellation_fee,
        "fare_type":        payload.fare_type,
        "departure_date":   payload.departure_date,
        "webhook_url":      payload.webhook_url,
    })

    prism_triggered = False
    if payload.current_price is not None:
        net_savings = payload.original_price - payload.current_price - payload.cancellation_fee
        if net_savings > 0:
            asyncio.create_task(_trigger_prism(
                tracking_id, payload.current_price, payload.seats_remaining, agency,
            ))
            prism_triggered = True

    response = {
        "tracking_id":      tracking_id,
        "status":           "monitoring",
        "agency":           agency,
        "route":            f"{payload.origin}-{payload.destination}",
        "fare_type":        payload.fare_type,
        "departure_date":   payload.departure_date,
        "original_price":   payload.original_price,
        "current_price":    payload.current_price,
        "cancellation_fee": payload.cancellation_fee,
        "webhook_url":      payload.webhook_url,
        "prism_triggered":  prism_triggered,
        "submitted_at":     now_iso,
    }

    if idempotency_key:
        _bookings.set_idempotency(idempotency_key, tracking_id, response)

    return response


# ---------------------------------------------------------------------------
# GET /bookings
# ---------------------------------------------------------------------------

@router.get("/bookings")
async def list_bookings(
    status_filter: Optional[str] = None,
    auth: tuple = Depends(require_api_key),
):
    agency, _ = auth
    rows = _bookings.get_by_agency(agency)
    if status_filter == "monitoring":
        rows = [r for r in rows if r.get("_active")]
    elif status_filter == "stopped":
        rows = [r for r in rows if not r.get("_active")]
    return {"agency": agency, "count": len(rows), "bookings": [_booking_to_response(r) for r in rows]}


# ---------------------------------------------------------------------------
# GET /bookings/{id}
# ---------------------------------------------------------------------------

@router.get("/bookings/{tracking_id}")
async def get_booking(tracking_id: str, auth: tuple = Depends(require_api_key)):
    agency, _ = auth
    rows  = _bookings.get_by_agency(agency)
    match = next((r for r in rows if r["booking_id"] == tracking_id), None)
    if not match:
        raise HTTPException(status_code=404, detail="Booking not found.")
    return _booking_to_response(match)


# ---------------------------------------------------------------------------
# GET /bookings/{id}/audit
# ---------------------------------------------------------------------------

@router.get("/bookings/{tracking_id}/audit")
async def get_booking_audit(tracking_id: str, auth: tuple = Depends(require_api_key)):
    agency, _ = auth
    booking = _bookings.get_by_id(tracking_id)
    if not booking or booking.metadata.get("agency") != agency:
        raise HTTPException(status_code=404, detail="Booking not found.")
    trail = _bookings.get_audit(tracking_id)
    return {"tracking_id": tracking_id, "count": len(trail), "trail": trail}


# ---------------------------------------------------------------------------
# PATCH /bookings/{id}
# ---------------------------------------------------------------------------

class PatchBookingRequest(BaseModel):
    current_price:    Optional[float] = Field(None, gt=0)
    seats_remaining:  Optional[int]   = Field(None, ge=0)
    webhook_url:      Optional[str]   = None
    cancellation_fee: Optional[float] = Field(None, ge=0)
    cabin_class:      Optional[str]   = None
    passenger:        Optional[dict]  = None


@router.patch("/bookings/{tracking_id}")
async def patch_booking(
    tracking_id: str,
    payload: PatchBookingRequest,
    auth: tuple = Depends(require_api_key),
):
    agency, _ = auth
    booking = _bookings.get_by_id(tracking_id)
    if not booking or booking.metadata.get("agency") != agency:
        raise HTTPException(status_code=404, detail="Booking not found.")

    changes: dict = {}

    if payload.webhook_url is not None:
        changes["webhook_url"] = payload.webhook_url
        booking.notify_webhook = payload.webhook_url

    if payload.cancellation_fee is not None:
        if payload.cancellation_fee >= booking.original_price:
            raise HTTPException(status_code=422, detail="cancellation_fee must be less than original_price.")
        changes["cancellation_fee"] = payload.cancellation_fee
        booking.cancellation_fee = payload.cancellation_fee

    if payload.cabin_class is not None:
        changes["cabin_class"] = payload.cabin_class
        booking.cabin_class = payload.cabin_class

    if payload.passenger:
        p = payload.passenger
        changes["passenger"] = p
        booking.passenger = Passenger(
            title=p.get("title", booking.passenger.title),
            given_name=p.get("given_name", booking.passenger.given_name),
            family_name=p.get("family_name", booking.passenger.family_name),
            born_on=p.get("born_on", booking.passenger.born_on),
            gender=p.get("gender", booking.passenger.gender),
            email=p.get("email", booking.passenger.email),
            phone=p.get("phone", booking.passenger.phone),
        )

    if payload.current_price is not None:
        changes["current_price"] = payload.current_price
        booking.metadata["current_price"] = payload.current_price

    if payload.seats_remaining is not None:
        changes["seats_remaining"] = payload.seats_remaining
        booking.metadata["seats_remaining"] = payload.seats_remaining

    updated_at = datetime.utcnow().isoformat() + "Z"
    booking.metadata["updated_at"] = updated_at
    _bookings.upsert(booking)
    _bookings.log_audit(tracking_id, agency, "updated", {"changes": changes, "updated_at": updated_at})

    prism_triggered = False
    prism_skipped_reason = None
    if payload.current_price is not None:
        net_savings = booking.original_price - payload.current_price - booking.cancellation_fee
        if net_savings <= 0:
            prism_skipped_reason = "no_savings"
        else:
            last_price   = float(booking.metadata.get("last_evaluated_price") or 0)
            last_eval_at = booking.metadata.get("last_evaluated_at", "")
            same_price   = abs(payload.current_price - last_price) < 0.01
            recent       = last_eval_at and _seconds_since(last_eval_at) < _PATCH_COOLDOWN

            if same_price and recent:
                prism_skipped_reason = "duplicate_price_cooldown"
            else:
                seats = (
                    payload.seats_remaining
                    if payload.seats_remaining is not None
                    else int(booking.metadata.get("seats_remaining") or 9)
                )
                asyncio.create_task(_trigger_prism(
                    tracking_id, payload.current_price, seats, agency,
                ))
                prism_triggered = True

    return {
        "ok":                   True,
        "tracking_id":          tracking_id,
        "updated":              updated_at,
        "prism_triggered":      prism_triggered,
        "prism_skipped_reason": prism_skipped_reason,
    }


# ---------------------------------------------------------------------------
# DELETE /bookings/{id}
# ---------------------------------------------------------------------------

@router.delete("/bookings/{tracking_id}")
async def delete_booking(tracking_id: str, auth: tuple = Depends(require_api_key)):
    agency, _ = auth
    booking = _bookings.get_by_id(tracking_id)
    if not booking or booking.metadata.get("agency") != agency:
        raise HTTPException(status_code=404, detail="Booking not found.")
    _bookings.deactivate(tracking_id)
    _bookings.log_audit(tracking_id, agency, "stopped", {"stopped_at": datetime.utcnow().isoformat() + "Z"})
    return {"ok": True, "tracking_id": tracking_id, "status": "stopped"}


# ---------------------------------------------------------------------------
# GET /analytics
# ---------------------------------------------------------------------------

@router.get("/analytics")
async def analytics(auth: tuple = Depends(require_api_key)):
    agency, api_key = auth
    rows    = _bookings.get_by_agency(agency)
    active  = [r for r in rows if r.get("_active")]
    stopped = [r for r in rows if not r.get("_active")]
    savings = _bookings.get_agency_savings(agency)
    ag_obj  = _agencies.get_by_key(api_key)
    return {
        "agency":               agency,
        "total_monitored":      len(rows),
        "currently_monitoring": len(active),
        "stopped":              len(stopped),
        "total_savings_usd":    round(savings, 2),
        "ryde_fees_usd":        round(savings * 0.20, 2),
        "net_savings_usd":      round(savings * 0.80, 2),
        "total_api_calls":      ag_obj.total_calls if ag_obj else 0,
        "last_api_call":        ag_obj.last_call_at if ag_obj else None,
        "rate_limit":           f"{_RATE_LIMIT} requests / {_RATE_WINDOW}s",
    }


# ---------------------------------------------------------------------------
# GET /account
# ---------------------------------------------------------------------------

@router.get("/account")
async def account(auth: tuple = Depends(require_api_key)):
    agency, api_key = auth
    ag = _agencies.get_by_key(api_key)
    return {
        "agency":             ag.name,
        "email":              ag.email,
        "environment":        ag.environment,
        "key_prefix":         api_key[:20] + "...",
        "member_since":       ag.created_at,
        "total_calls":        ag.total_calls,
        "last_call":          ag.last_call_at,
        "rate_limit":         f"{_RATE_LIMIT} req / {_RATE_WINDOW}s",
        "billing_configured": ag.stripe_customer_id is not None,
        "endpoints": [
            "POST   /api/v1/predict",
            "POST   /api/v1/monitor",
            "GET    /api/v1/bookings",
            "GET    /api/v1/bookings/{id}",
            "GET    /api/v1/bookings/{id}/audit",
            "PATCH  /api/v1/bookings/{id}",
            "DELETE /api/v1/bookings/{id}",
            "GET    /api/v1/analytics",
            "GET    /api/v1/account",
        ],
    }
