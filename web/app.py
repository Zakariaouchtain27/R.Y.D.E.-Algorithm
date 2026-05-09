import os
import uuid
from datetime import datetime
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

_SUCCESS_FEE = float(os.getenv("SUCCESS_FEE_PERCENT", "20")) / 100
_BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def _get_stripe() -> StripeClient:
    global _stripe
    if _stripe is None:
        key = os.getenv("STRIPE_SECRET_KEY", "")
        if not key:
            raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY not configured.")
        _stripe = StripeClient(key)
    return _stripe


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


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

    # Write booking into the bot's store — PriceMonitor picks it up automatically
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


@app.post("/webhook/ryde")
async def ryde_webhook(request: Request):
    payload = await request.json()
    event = payload.get("event")
    booking_id = payload.get("booking_id")

    if event == "ryde.rebooking" and payload.get("success"):
        savings = float(payload.get("savings_realized", 0))
        client = _clients.get_client(booking_id)
        if client and client.get("stripe_payment_method") and savings > 0:
            fee = round(savings * _SUCCESS_FEE, 2)
            try:
                _get_stripe().charge(
                    customer_id=client["stripe_customer_id"],
                    payment_method_id=client["stripe_payment_method"],
                    amount_usd=fee,
                    description=f"RYDE saved you ${savings:.2f} on your flight",
                )
                _clients.add_savings(booking_id, savings)
            except Exception as exc:
                print(f"[ryde.webhook] Stripe charge failed: {exc}")

    if booking_id:
        _clients.log_event(booking_id, event, payload)

    return {"ok": True}
