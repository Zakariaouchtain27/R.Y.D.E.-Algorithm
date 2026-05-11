import os
import sqlite3
import uuid
from datetime import datetime
from threading import Lock
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ryde.models import Booking, Passenger
from ryde.store import BookingStore
from .client_store import ClientStore
from .stripe_client import StripeClient

app = FastAPI(title="RYDE")
templates = Jinja2Templates(directory="web/templates")

_db_path = os.getenv("RYDE_DB_PATH", "ryde.db")
_clients = ClientStore(_db_path)
_bookings = BookingStore(_db_path)
_stripe: Optional[StripeClient] = None
_key_lock = Lock()

_BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")
_LS_STARTER_URL = os.getenv("LS_STARTER_URL", "")
_LS_PRO_URL = os.getenv("LS_PRO_URL", "")


def _get_stripe() -> StripeClient:
    global _stripe
    if _stripe is None:
        key = os.getenv("STRIPE_SECRET_KEY", "")
        if not key:
            raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY not configured.")
        _stripe = StripeClient(key)
    return _stripe


def _ensure_free_agencies_table():
    conn = sqlite3.connect(_db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS free_agencies (
            agency_id  TEXT PRIMARY KEY,
            name       TEXT NOT NULL,
            email      TEXT NOT NULL UNIQUE,
            api_key    TEXT NOT NULL UNIQUE,
            status     TEXT NOT NULL DEFAULT 'active',
            plan       TEXT NOT NULL DEFAULT 'free',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


_ensure_free_agencies_table()


# ---------------------------------------------------------------------------
# Routes — Public marketing
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/pricing", response_class=HTMLResponse)
async def pricing(request: Request):
    return templates.TemplateResponse("pricing.html", {
        "request": request,
        "ls_starter_url": _LS_STARTER_URL,
        "ls_pro_url": _LS_PRO_URL,
    })


@app.get("/signup", response_class=HTMLResponse)
async def signup_form(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request})


@app.post("/signup", response_class=HTMLResponse)
async def signup_submit(
    request: Request,
    agency_name: str = Form(...),
    email: str = Form(...),
):
    conn = sqlite3.connect(_db_path)
    existing = conn.execute(
        "SELECT agency_id FROM free_agencies WHERE email = ?", (email,)
    ).fetchone()
    conn.close()

    if existing:
        return templates.TemplateResponse("signup.html", {
            "request": request,
            "error": "An account with this email already exists.",
        }, status_code=400)

    api_key = "ryde_free_" + uuid.uuid4().hex
    agency_id = str(uuid.uuid4())

    with _key_lock:
        conn = sqlite3.connect(_db_path)
        conn.execute(
            "INSERT INTO free_agencies (agency_id, name, email, api_key) VALUES (?, ?, ?, ?)",
            (agency_id, agency_name, email, api_key),
        )
        conn.commit()
        conn.close()

    return templates.TemplateResponse("free_key.html", {
        "request": request,
        "api_key": api_key,
    })


@app.get("/api")
async def api_docs():
    return RedirectResponse(
        "https://github.com/Zakariaouchtain27/R.Y.D.E.-Algorithm/blob/main/INTEGRATION_GUIDE.md"
    )


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ---------------------------------------------------------------------------
# Routes — Legacy B2C (kept for backward compatibility)
# ---------------------------------------------------------------------------

@app.get("/register", response_class=HTMLResponse)
async def register_form(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})


@app.post("/register")
async def register_submit(
    name: str = Form(...),
    email: str = Form(...),
    origin: str = Form(...),
    destination: str = Form(...),
    departure_date: str = Form(...),
    original_price: float = Form(...),
    cancellation_fee: float = Form(...),
    booking_ref: str = Form(...),
    seat_preference: str = Form("cheapest"),
):
    client_id = str(uuid.uuid4())
    customer = _get_stripe().create_customer(email=email, name=name)

    _clients.create_client(
        client_id=client_id,
        name=name,
        email=email,
        stripe_customer_id=customer.id,
        booking_data={
            "origin": origin.upper().strip(),
            "destination": destination.upper().strip(),
            "departure_date": departure_date,
            "original_price": original_price,
            "cancellation_fee": cancellation_fee,
            "booking_ref": booking_ref.strip(),
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
    return templates.TemplateResponse("payment.html", {
        "request": request,
        "client_id": client_id,
        "client_secret": setup_intent.client_secret,
        "publishable_key": os.getenv("STRIPE_PUBLISHABLE_KEY", ""),
        "booking": client["booking_data"],
    })


@app.post("/setup-payment/{client_id}/confirm")
async def confirm_setup(
    client_id: str,
    payment_method_id: str = Form(...),
):
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
            title="mr",
            given_name=name_parts[0],
            family_name=name_parts[-1] if len(name_parts) > 1 else name_parts[0],
            born_on="1990-01-01",
            gender="m",
            email=client["email"],
            phone="+10000000000",
        ),
        origin=bd["origin"],
        destination=bd["destination"],
        departure_date=datetime.strptime(bd["departure_date"], "%Y-%m-%d"),
        original_price=float(bd["original_price"]),
        currency="USD",
        cancellation_fee=float(bd["cancellation_fee"]),
        adapter="duffel",
        adapter_booking_ref=bd["booking_ref"],
        notify_webhook=f"{_BASE_URL}/webhook/ryde",
    )
    _bookings.upsert(booking)

    return RedirectResponse(f"/success/{client_id}", status_code=303)


@app.get("/success/{client_id}", response_class=HTMLResponse)
async def success_page(request: Request, client_id: str):
    client = _clients.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("success.html", {
        "request": request,
        "client": client,
    })


@app.get("/dashboard/{client_id}", response_class=HTMLResponse)
async def dashboard(request: Request, client_id: str):
    client = _clients.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404)
    events = _clients.get_events(client_id)
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "client": client,
        "events": events,
    })


# ---------------------------------------------------------------------------
# Webhook — PRISM decisions forwarded from RYDE engine
# ---------------------------------------------------------------------------

@app.post("/webhook/ryde")
async def ryde_webhook(request: Request):
    payload = await request.json()
    event = payload.get("event")
    booking_id = payload.get("booking_id")

    if event == "ryde.rebooking" and payload.get("success"):
        savings = float(payload.get("savings_realized", 0))
        client = _clients.get_client(booking_id)
        if client and savings > 0:
            # Record savings for dashboard display — billing handled via subscription
            _clients.add_savings(booking_id, savings)

    if booking_id:
        _clients.log_event(booking_id, event, payload)

    return {"ok": True}
