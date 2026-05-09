"""
PRISM — Probabilistic Rebooking via Iterated Stochastic Modeling

First application of Longstaff-Schwartz Monte Carlo Optimal Stopping
to commercial flight rebooking. Replaces the RYDE v2 heuristic engine
with a mathematically provable expected-value decision framework.

Decision framework:
    expected_value  = LSMC(5,000 OU price paths, original_price, fee)
    intrinsic       = original_price − current_price − fee
    ratio           = intrinsic / expected_value

    ratio ≥ 0.95  →  STRIKE       (rebook — ≥95% of provable value captured)
    ratio ≥ 0.70  →  PHANTOM_HOLD (lock fare — 70-94% captured)
    ratio <  0.70  →  WAIT         (better deal statistically probable)

    Overrides:
      Load factor pressure >78%  → downgrade STRIKE → PHANTOM_HOLD
      Cascade probability >72%   → upgrade WAIT → PHANTOM_HOLD
"""

import logging
import math
from datetime import datetime
from typing import Optional

import numpy as np

from ..models import Booking, PriceSnapshot, RYDEAction, RYDEDecision
from .competitive import CompetitiveCascadeDetector, LoadFactorPressureModel
from .lsmc import LSMCOptimalStopper
from .price_history import PriceHistory, make_route_key
from .stochastic import OrnsteinUhlenbeck

log = logging.getLogger(__name__)


class PRISMEngine:
    """
    Drop-in replacement for RegretMinimizationEngine.
    Returns the identical RYDEDecision dataclass — RYDEBot needs zero changes.
    """

    STRIKE_RATIO = 0.95
    HOLD_RATIO = 0.70
    LF_OVERRIDE_PROB = 0.78
    CASCADE_HOLD_PROB = 0.72
    N_SIMULATIONS = 5000

    def __init__(
        self,
        db_path: str = "ryde.db",
        n_simulations: int = N_SIMULATIONS,
        rng_seed: Optional[int] = None,
    ):
        self.history = PriceHistory(db_path)
        self.ou = OrnsteinUhlenbeck()
        self.lsmc = LSMCOptimalStopper()
        self.cascade = CompetitiveCascadeDetector()
        self.load_factor = LoadFactorPressureModel()
        self.n_simulations = n_simulations
        self._rng = np.random.default_rng(rng_seed)

    def evaluate(
        self,
        booking: Booking,
        snapshot: PriceSnapshot,
        historical_max_drop: Optional[float] = None,
        n_competitors_dropped: int = 0,
    ) -> RYDEDecision:
        """
        Evaluate the optimal rebooking decision for one booking.
        Records the snapshot to price history automatically.
        """
        days = max(0, (booking.departure_date - datetime.now()).days)
        net_savings = (
            booking.original_price
            - snapshot.current_price
            - booking.cancellation_fee
        )

        route_key = make_route_key(
            booking.origin,
            booking.destination,
            booking.departure_date.strftime("%Y-%m-%d"),
        )
        self.history.record_snapshot(
            route_key,
            snapshot.current_price,
            seats_remaining=snapshot.seats_remaining,
            days_to_dep=days,
        )

        if net_savings <= 0:
            return RYDEDecision(
                action=RYDEAction.IGNORE,
                confidence_score=0.0,
                net_savings=round(net_savings, 2),
                expected_future_gain=0.0,
                probability_of_future_drop=0.0,
                seat_urgency_multiplier=1.0,
                reasoning="Current price ≥ original price after fees — nothing to rebook.",
            )

        price_series = self.history.get_price_series(route_key)
        self.ou.fit(price_series)

        reference_price = (
            self.history.get_reference_price(route_key) or snapshot.current_price
        )

        paths = self.ou.simulate_paths(
            current_price=snapshot.current_price,
            reference_price=reference_price,
            days=max(days, 1),
            n_paths=self.n_simulations,
            rng=self._rng,
            volatility_multiplier=booking.volatility_index,
        )

        expected_value, _ = self.lsmc.compute(
            price_paths=paths,
            original_price=booking.original_price,
            cancellation_fee=booking.cancellation_fee,
        )

        ratio = net_savings / expected_value if expected_value > 0 else 1.0

        booking_velocity = self.history.get_booking_velocity(route_key)
        lf_pressure = self.load_factor.pressure(
            seats_remaining=snapshot.seats_remaining,
            days_to_departure=days,
            observed_velocity=booking_velocity,
        )

        cascade_prob = self.cascade.cascade_probability(n_competitors_dropped)

        if ratio >= self.STRIKE_RATIO:
            action = RYDEAction.STRIKE
        elif ratio >= self.HOLD_RATIO:
            action = RYDEAction.PHANTOM_HOLD
        else:
            action = RYDEAction.WAIT

        if action == RYDEAction.STRIKE and lf_pressure > self.LF_OVERRIDE_PROB:
            action = RYDEAction.PHANTOM_HOLD
            log.info(
                "%s: LF override (pressure=%.0f%%) → PHANTOM_HOLD",
                booking.booking_id, lf_pressure * 100,
            )

        if action == RYDEAction.WAIT and cascade_prob > self.CASCADE_HOLD_PROB:
            action = RYDEAction.PHANTOM_HOLD
            log.info(
                "%s: Cascade upgrade (P=%.0f%%) → PHANTOM_HOLD",
                booking.booking_id, cascade_prob * 100,
            )

        seat_urgency = min(2.5, 1.0 + 1.5 * math.exp(-snapshot.seats_remaining / 4))
        prob_further_drop = round(max(0.0, 1.0 - ratio) * 100.0, 1)

        reasoning = (
            f"LSMC E[value]=${expected_value:.2f} | intrinsic=${net_savings:.2f} | "
            f"ratio={ratio:.2f} | LF={lf_pressure:.0%} | cascade={cascade_prob:.0%} | "
            f"κ={self.ou.kappa:.3f} σ={self.ou.sigma:.1f} | days={days}"
        )

        return RYDEDecision(
            action=action,
            confidence_score=round(min(ratio, 1.0) * 100, 1),
            net_savings=round(net_savings, 2),
            expected_future_gain=round(max(expected_value - net_savings, 0.0), 2),
            probability_of_future_drop=prob_further_drop,
            seat_urgency_multiplier=round(seat_urgency, 3),
            reasoning=reasoning,
        )
