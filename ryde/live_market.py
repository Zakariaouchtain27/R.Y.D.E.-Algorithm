"""
Live flight-market simulator.

Maintains a mean-reverting price walk for 10 popular routes and
publishes a {type: "market", ...} event every tick. The landing page
subscribes via WebSocket and renders them stock-ticker style.

The publish format is identical to what a real Amadeus/Duffel poller
would emit, so swapping in live data later is a one-function change
(replace `_tick` with a call to AmadeusAdapter.search()).
"""
import asyncio
import logging
import random
from collections import deque
from datetime import datetime
from typing import Deque, Dict, List, Optional

from . import events

log = logging.getLogger(__name__)

#  origin, destination, base USD, daily volatility (fraction of base)
ROUTES = [
    ("JFK", "CDG",  850, 0.022),
    ("LHR", "JFK",  720, 0.020),
    ("LAX", "NRT",  950, 0.028),
    ("SFO", "SIN", 1180, 0.032),
    ("DXB", "LHR",  580, 0.018),
    ("MIA", "MAD",  480, 0.020),
    ("ORD", "FRA",  670, 0.022),
    ("SEA", "AMS",  750, 0.024),
    ("BOS", "CDG",  620, 0.021),
    ("ATL", "LHR",  690, 0.023),
]

HISTORY_LEN = 30


class LiveMarket:
    def __init__(self) -> None:
        self._prices: Dict[str, float] = {}
        self._opens: Dict[str, float] = {}
        self._history: Dict[str, Deque[float]] = {}
        self._meta: Dict[str, dict] = {}
        for o, d, base, vol in ROUTES:
            key = f"{o}-{d}"
            self._prices[key] = float(base)
            self._opens[key] = float(base)
            self._history[key] = deque([float(base)], maxlen=HISTORY_LEN)
            self._meta[key] = {"origin": o, "destination": d, "base": float(base), "vol": vol}
        self._running = False

    async def run(self, interval: float = 3.0) -> None:
        self._running = True
        log.info("LiveMarket started: %d routes, interval=%.1fs", len(ROUTES), interval)
        while self._running:
            try:
                self._tick()
            except Exception as exc:
                log.warning("market tick failed: %s", exc)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False

    def snapshot(self) -> List[dict]:
        """Initial payload for newly connected clients — one event per route."""
        return [self._payload(k) for k in self._prices.keys()]

    def _tick(self) -> None:
        ts = datetime.utcnow().isoformat() + "Z"
        for key, meta in self._meta.items():
            base = meta["base"]
            vol = meta["vol"]
            old = self._prices[key]

            # Mean-reverting random walk + occasional jump (5% chance)
            mr = 0.04 * (base - old)
            noise = random.gauss(0, base * vol)
            jump = random.gauss(0, base * 0.05) if random.random() < 0.05 else 0.0
            new = old + mr + noise + jump
            new = max(min(new, base * 1.40), base * 0.65)
            new = round(new, 2)

            self._prices[key] = new
            self._history[key].append(new)
            events.publish(self._payload(key, ts=ts, old=old))

    def _payload(self, key: str, ts: Optional[str] = None, old: Optional[float] = None) -> dict:
        meta = self._meta[key]
        new = self._prices[key]
        opn = self._opens[key]
        change = new - opn
        pct = (change / opn) * 100.0 if opn else 0.0
        return {
            "type": "market",
            "route": key,
            "origin": meta["origin"],
            "destination": meta["destination"],
            "current_price": new,
            "open_price": round(opn, 2),
            "old_price": round(old, 2) if old is not None else None,
            "change": round(change, 2),
            "change_pct": round(pct, 2),
            "history": list(self._history[key]),
            "timestamp": ts or (datetime.utcnow().isoformat() + "Z"),
        }


_market = LiveMarket()


def get_market() -> LiveMarket:
    return _market
