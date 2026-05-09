"""
RYDE demo — runs the engine across 6 realistic scenarios without live API calls.
No API keys required.

    python demo.py
"""
from datetime import datetime, timedelta

from ryde.engine import RegretMinimizationEngine
from ryde.models import Booking, Passenger, PriceSnapshot

engine = RegretMinimizationEngine()

passenger = Passenger(
    title="mr", given_name="John", family_name="Doe",
    born_on="1990-01-01", gender="m",
    email="john@example.com", phone="+15551234567",
)

SCENARIOS = [
    {
        "label": "Far out, big drop, 2 seats left (volatile route)",
        "days": 120, "original": 900, "current": 600,
        "fee": 50, "seats": 2, "vol": 1.8,
    },
    {
        "label": "Sweet spot (~42d), near max-drop, many seats",
        "days": 42, "original": 850, "current": 530,
        "fee": 50, "seats": 20, "vol": 1.0,
    },
    {
        "label": "Sweet spot, moderate drop, 5 seats",
        "days": 38, "original": 700, "current": 580,
        "fee": 50, "seats": 5, "vol": 1.0,
    },
    {
        "label": "14 days out, decent drop, 3 seats",
        "days": 14, "original": 620, "current": 490,
        "fee": 50, "seats": 3, "vol": 1.2,
    },
    {
        "label": "5 days out, small drop, 1 seat (last-minute)",
        "days": 5, "original": 500, "current": 460,
        "fee": 50, "seats": 1, "vol": 1.0,
    },
    {
        "label": "No savings after fee (should IGNORE)",
        "days": 30, "original": 600, "current": 580,
        "fee": 50, "seats": 10, "vol": 1.0,
    },
]

HISTORICAL_MAX_DROP = 350.0

print("\n" + "=" * 65)
print("  R.Y.D.E. v2  —  Decision Engine Demo")
print("=" * 65)

for s in SCENARIOS:
    dep = datetime.now() + timedelta(days=s["days"])
    booking = Booking(
        booking_id="demo-001",
        passenger=passenger,
        origin="JFK",
        destination="CDG",
        departure_date=dep,
        original_price=s["original"],
        currency="USD",
        cancellation_fee=s["fee"],
        adapter="duffel",
        adapter_booking_ref="ord_demo123",
        volatility_index=s["vol"],
    )
    snapshot = PriceSnapshot(
        booking_id="demo-001",
        current_price=s["current"],
        seats_remaining=s["seats"],
        snapshot_time=datetime.now(),
        fare_id="off_demo_xyz",
        source="demo",
    )
    d = engine.evaluate(booking, snapshot, historical_max_drop=HISTORICAL_MAX_DROP)

    print(f"\nScenario : {s['label']}")
    print(f"Days out : {s['days']}d  |  "
          f"Price: ${s['original']} → ${s['current']}  |  "
          f"Fee: ${s['fee']}  |  Seats: {s['seats']}  |  Vol: {s['vol']}x")
    print(f"Action   : {d.action}")
    print(f"Score    : {d.confidence_score}  |  Net savings: ${d.net_savings}  |  "
          f"E[future gain]: ${d.expected_future_gain}")
    print(f"P(drop)  : {d.probability_of_future_drop}%  |  "
          f"Seat urgency: {d.seat_urgency_multiplier}x")
    print(f"Reason   : {d.reasoning}")

print("\n" + "=" * 65)
