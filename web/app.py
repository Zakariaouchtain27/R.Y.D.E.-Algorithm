import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

from fastapi import FastAPI, Form, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ryde.logging_config import setup_logging
setup_logging(os.getenv("LOG_LEVEL", "INFO"))

from ryde import events as ryde_events
from ryde.live_market import get_market
from ryde.models import Booking, Passenger
from ryde.price_monitor import PriceMonitor
from ryde.store import BookingStore
from ryde.agency_store import AgencyStore
from .admin import router as admin_router
from .api_v1 import router as api_v1_router, scan_all_active
from .lemon import router as lemon_router
from .client_store import ClientStore
from .stripe_client import StripeClient

log = logging.getLogger(__name__)

# The app is wired with lifespan=_lifespan so that startup/shutdown
# run correctly on Railway. Routers are attached after the lifespan is defined.

_db_path  = os.getenv("RYDE_DB_PATH", "ryde.db")
_clients  = ClientStore(_db_path)
_bookings = BookingStore(_db_path)
_agencies = AgencyStore(_db_path)
_stripe: Optional[StripeClient] = None

_BASE_URL        = os.getenv("BASE_URL", "http://localhost:8000")
_MARKET_INTERVAL = float(os.getenv("MARKET_TICK_SECONDS", "3"))
_SCAN_INTERVAL   = int(os.getenv("PRISM_SCAN_INTERVAL_SECONDS", "900"))  # 15 min default

# Signal API model — PriceMonitor drives time-decay re-evaluations only.
# No external adapter polling. Instantiated during lifespan.
_price_monitor: Optional[PriceMonitor] = None


def _get_stripe() -> StripeClient:
    global _stripe
    if _stripe is None:
        key = os.getenv("STRIPE_SECRET_KEY", "")
        if not key:
            raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY not configured.")
        _stripe = StripeClient(key)
    return _stripe


# ---------------------------------------------------------------------------
# WebSocket fan-out
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        async with self._lock:
            targets = list(self._clients)
        dead = []
        for ws in targets:
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


_manager = ConnectionManager()
_loop: Optional[asyncio.AbstractEventLoop] = None
_market_task: Optional[asyncio.Task] = None
_scan_task:   Optional[asyncio.Task] = None


def _on_ryde_event(event: dict) -> None:
    if _loop is None or _loop.is_closed():
        return
    booking_id = event.get("booking_id", "")
    event_type = event.get("type", "decision")
    if booking_id and event_type != "market":
        try:
            booking = _bookings.get_by_id(booking_id)
            agency  = booking.metadata.get("agency", "") if booking else ""
            _bookings.log_audit(booking_id, agency, event_type, event)
        except Exception:
            pass
    asyncio.run_coroutine_threadsafe(_manager.broadcast(event), _loop)


async def _prism_background_scan() -> None:
    """
    Signal API time-decay scanner — no external API calls.
    Runs every PRISM_SCAN_INTERVAL_SECONDS (default 15 min).
    Per-booking cadence inside scan_all_active() based on days_to_departure:
      >= 14 days  →  every 60 min
       7–14 days  →  every 30 min
        < 7 days  →  every 15 min
    Bookings evaluated too recently for their cadence tier are skipped.
    """
    await asyncio.sleep(60)   # warm-up: let the server fully start first
    while True:
        try:
            count = await scan_all_active()
            if count:
                log.info("PRISM background scan: %d booking(s) evaluated", count)
            else:
                log.debug("PRISM background scan: no bookings due for evaluation")
        except Exception as exc:
            log.error("PRISM background scan failed: %s", exc, exc_info=True)
        await asyncio.sleep(_SCAN_INTERVAL)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """FastAPI lifespan — replaces deprecated @on_event startup/shutdown hooks."""
    global _loop, _market_task, _scan_task, _price_monitor

    # ── Startup ────────────────────────────────────────────────────────────
    Path(_db_path).parent.mkdir(parents=True, exist_ok=True)

    db_url = os.getenv("DATABASE_URL", "")
    if db_url:
        for attempt in range(10):
            try:
                with _bookings._lock:
                    _bookings._execute("SELECT 1").fetchone()
                break
            except Exception as exc:
                if attempt == 9:
                    log.error(
                        "PostgreSQL not reachable after 10 attempts: %s", exc,
                        exc_info=True,
                    )
                    raise
                wait = 2 ** attempt
                log.warning(
                    "PostgreSQL not ready (attempt %d/10), retrying in %ds",
                    attempt + 1, wait,
                )
                await asyncio.sleep(wait)

    # Signal API model: PriceMonitor re-evaluates stored prices only.
    # No DuffelAdapter / AmadeusAdapter required.
    _price_monitor = PriceMonitor(store=_bookings, scan_interval=_SCAN_INTERVAL)
    _price_monitor.start()

    _loop        = asyncio.get_running_loop()
    ryde_events.subscribe(_on_ryde_event)
    _market_task = asyncio.create_task(get_market().run(interval=_MARKET_INTERVAL))
    _scan_task   = asyncio.create_task(_prism_background_scan())

    log.info(
        "RYDE startup complete",
        extra={"db": "postgres" if db_url else "sqlite", "scan_interval_s": _SCAN_INTERVAL},
    )

    yield  # ── Server is running ──────────────────────────────────────────

    # ── Shutdown ───────────────────────────────────────────────────────────
    log.info("RYDE shutting down…")
    if _price_monitor is not None:
        _price_monitor.stop()
    ryde_events.unsubscribe(_on_ryde_event)
    get_market().stop()
    if _market_task is not None:
        _market_task.cancel()
        try:
            await _market_task
        except asyncio.CancelledError:
            pass
    if _scan_task is not None:
        _scan_task.cancel()
        try:
            await _scan_task
        except asyncio.CancelledError:
            pass
    log.info("RYDE shutdown complete.")

# Wire the lifespan into the app after it's defined
app = FastAPI(title="RYDE", lifespan=_lifespan)
app.include_router(api_v1_router)
app.include_router(admin_router)
app.include_router(lemon_router)
template_dir = os.getenv("RYDE_TEMPLATE_DIR", "web/templates")
templates = Jinja2Templates(directory=template_dir)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    checks: dict = {}
    try:
        with _bookings._lock:
            _bookings._execute("SELECT 1").fetchone()
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {exc}"
    checks["market"]     = "ok" if (_market_task and not _market_task.done()) else "stopped"
    checks["prism_scan"] = "ok" if (_scan_task   and not _scan_task.done())   else "stopped"
    overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content={"status": overall, "checks": checks, "timestamp": datetime.utcnow().isoformat() + "Z"},
    )


@app.websocket("/ws/ticker")
async def ws_ticker(ws: WebSocket):
    await _manager.connect(ws)
    try:
        await ws.send_json({"type": "hello", "timestamp": datetime.utcnow().isoformat() + "Z"})
        for snap in get_market().snapshot():
            await ws.send_json(snap)
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await _manager.disconnect(ws)
    except Exception:
        await _manager.disconnect(ws)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse(request, "index.html")

@app.get("/terminal", response_class=HTMLResponse)
async def terminal(request: Request):
    return templates.TemplateResponse(request, "terminal.html")

@app.get("/api", response_class=HTMLResponse)
async def api_docs(request: Request):
    return templates.TemplateResponse(request, "api_docs.html", {
        "base_url": os.getenv("BASE_URL", "https://your-app.railway.app"),
    })

@app.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request):
    return templates.TemplateResponse(request, "pricing.html", {
        "free_url":    os.getenv("LS_FREE_URL", "/signup"),
        "starter_url": os.getenv("LS_STARTER_URL", ""),
        "pro_url":     os.getenv("LS_PRO_URL", ""),
    })

@app.get("/welcome", response_class=HTMLResponse)
async def welcome(request: Request):
    return templates.TemplateResponse(request, "welcome.html")


# ---------------------------------------------------------------------------
# Free-tier self-serve signup
# ---------------------------------------------------------------------------

@app.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    return templates.TemplateResponse(request, "signup.html", {"error": ""})


@app.post("/signup", response_class=HTMLResponse)
async def signup_submit(
    request: Request,
    name:  str = Form(...),
    email: str = Form(...),
):
    name  = name.strip()
    email = email.strip().lower()
    if not name or not email or "@" not in email:
        return templates.TemplateResponse(request, "signup.html", {
            "error": "Please enter a valid agency name and email address."
        }, status_code=422)
    try:
        agency = _agencies.create_agency(name=name, email=email, environment="test")
    except Exception as exc:
        log.error("Free signup failed", extra={"email": email, "error": str(exc)})
        return templates.TemplateResponse(request, "signup.html", {
            "error": "Something went wrong. Please try again or email api@ryde.io."
        }, status_code=500)
    return templates.TemplateResponse(request, "free_key.html", {
        "agency_name": agency.name,
        "api_key":     agency.api_key,
    })


# ---------------------------------------------------------------------------
# B2C consumer registration flow
# ---------------------------------------------------------------------------

@app.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse(request, "register.html")


@app.post("/register")
async def register_submit(
    name: str = Form(...), email: str = Form(...),
    origin: str = Form(...), destination: str = Form(...),
    departure_date: str = Form(...), original_price: float = Form(...),
    cancellation_fee: float = Form(...), booking_ref: str = Form(...),
    seat_preference: str = Form("cheapest"),
):
    client_id = str(uuid.uuid4())
    customer = _get_stripe().create_customer(email=email, name=name)
    _clients.create_client(
        client_id=client_id, name=name, email=email,
        stripe_customer_id=customer.id,
        booking_data={
            "origin": origin.upper().strip(), "destination": destination.upper().strip(),
            "departure_date": departure_date, "original_price": original_price,
            "cancellation_fee": cancellation_fee, "booking_ref": booking_ref.strip(),
            "seat_preference": seat_preference,
        },
    )
    return RedirectResponse(f"/setup-payment/{client_id}", status_code=303)


@app.get("/setup-payment/{client_id}", response_class=HTMLResponse)
async def setup_payment_page(request: Request, client_id: str):
    client = _clients.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404)
    setup_intent = _get_stripe().create_setup_intent(client["stripe_customer_id"])
    return templates.TemplateResponse(request, "payment.html", {
        "client_id": client_id, "client_secret": setup_intent.client_secret,
        "publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", ""),
        "booking": client["booking_data"],
    })


@app.post("/setup-payment/{client_id}/confirm")
async def confirm_setup(client_id: str, payment_method_id: str = Form(...)):
    client = _clients.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404)
    _clients.save_payment_method(client_id, payment_method_id)
    _clients.activate_monitoring(client_id)
    bd = client["booking_data"]
    name_parts = client["name"].split()
    booking = Booking(
        booking_id=client_id,
        passenger=Passenger(
            title="mr", given_name=name_parts[0],
            family_name=name_parts[-1] if len(name_parts) > 1 else name_parts[0],
            born_on="1990-01-01", gender="m", email=client["email"], phone="+10000000000",
        ),
        origin=bd["origin"], destination=bd["destination"],
        departure_date=datetime.strptime(bd["departure_date"], "%Y-%m-%d"),
        original_price=float(bd["original_price"]), currency="USD",
        cancellation_fee=float(bd["cancellation_fee"]),
        adapter="duffel", adapter_booking_ref=bd["booking_ref"],
        notify_webhook=f"{_BASE_URL}/webhook/ryde",
    )
    _bookings.upsert(booking)
    return RedirectResponse(f"/success/{client_id}", status_code=303)


@app.get("/success/{client_id}", response_class=HTMLResponse)
async def success_page(request: Request, client_id: str):
    client = _clients.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "success.html", {"client": client})


@app.get("/dashboard/{client_id}", response_class=HTMLResponse)
async def dashboard(request: Request, client_id: str):
    client = _clients.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404)
    ev = _clients.get_events(client_id)
    return templates.TemplateResponse(request, "dashboard.html", {"client": client, "events": ev})


@app.post("/webhook/ryde")
async def ryde_webhook(request: Request):
    payload    = await request.json()
    event      = payload.get("event")
    booking_id = payload.get("booking_id")
    if event == "ryde.rebooking" and payload.get("success"):
        savings = float(payload.get("savings_realized", 0))
        client  = _clients.get_client(booking_id)
        if client and savings > 0:
            # Record savings for dashboard display only — billing is invoiced
            # manually at month-end via the agency's subscription plan.
            _clients.add_savings(booking_id, savings)
    if booking_id:
        _clients.log_event(booking_id, event, payload)
    return {"ok": True}
