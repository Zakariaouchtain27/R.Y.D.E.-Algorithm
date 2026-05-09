import math
from datetime import datetime
from typing import Optional

from .models import Booking, PriceSnapshot, RYDEAction, RYDEDecision


class RegretMinimizationEngine:
    """
    R.Y.D.E. v2 — Reverse-Yield Delta Engine

    Core decision model:
      strike_value = net_savings - E[future_gain]
      final_score  = normalize(strike_value) * seat_urgency * 100

    Thresholds (default):
      >= 72  → STRIKE       (rebook immediately)
      >= 48  → PHANTOM_HOLD (lock fare for 24 h, keep watching)
      <  48  → WAIT         (continue monitoring)
    """

    def __init__(
        self,
        strike_threshold: float = 72.0,
        phantom_hold_threshold: float = 48.0,
    ):
        self.strike_threshold = strike_threshold
        self.phantom_hold_threshold = phantom_hold_threshold

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(
        self,
        booking: Booking,
        snapshot: PriceSnapshot,
        historical_max_drop: Optional[float] = None,
    ) -> RYDEDecision:
        """
        Main evaluation entry point.

        historical_max_drop: the largest price drop ever observed for this
        route/season pair. Pass None when no history exists — the engine
        will fall back to a conservative estimate.
        """
        days = max(0, (booking.departure_date - datetime.now()).days)
        net_savings = (
            booking.original_price - snapshot.current_price - booking.cancellation_fee
        )

        if net_savings <= 0:
            return RYDEDecision(
                action=RYDEAction.IGNORE,
                confidence_score=0.0,
                net_savings=round(net_savings, 2),
                expected_future_gain=0.0,
                probability_of_future_drop=0.0,
                seat_urgency_multiplier=1.0,
                reasoning="No savings after fees — nothing to do.",
            )

        prob_drop = self._drop_probability(float(days), booking.volatility_index)

        # How much more could we save if we wait?
        if historical_max_drop and historical_max_drop > 0:
            remaining_room = max(0.0, historical_max_drop - net_savings)
        else:
            # No historical data: conservatively assume 30 % more might come
            remaining_room = net_savings * 0.30

        expected_future_gain = prob_drop * remaining_room

        # Net advantage of acting now vs waiting
        strike_value = net_savings - expected_future_gain

        # FIX (Bug 1 — “End of Time” trap):
        # When departure is imminent (prob_drop < 5%), there is no time left to
        # wait for a better deal. Normalizing against historical_max_drop here
        # would produce an artificially low score and result in a WAIT decision
        # even though positive savings are available right now. Force ceiling to
        # net_savings so the ratio is 1.0 and the score reflects pure urgency.
        if prob_drop < 0.05 and net_savings > 0:
            ceiling = net_savings
        else:
            ceiling = max(historical_max_drop or 0, net_savings, 1.0)

        normalized = max(0.0, min(strike_value / ceiling, 1.0))
        urgency = self._seat_urgency(snapshot.seats_remaining)
        strike_score = normalized * urgency * 100

        if strike_score >= self.strike_threshold:
            action = RYDEAction.STRIKE
            reasoning = (
                f"Rebook now. Net savings ${net_savings:.2f} exceed expected future gain "
                f"${expected_future_gain:.2f} with only {snapshot.seats_remaining} seat(s) left."
            )
        elif strike_score >= self.phantom_hold_threshold:
            action = RYDEAction.PHANTOM_HOLD
            reasoning = (
                f"Lock fare for 24 h. Moderate confidence — "
                f"{prob_drop * 100:.0f}% chance of further drop, but urgency warrants protection."
            )
        else:
            action = RYDEAction.WAIT
            reasoning = (
                f"Wait. {prob_drop * 100:.0f}% chance of a deeper drop. "
                f"Expected future gain ${expected_future_gain:.2f} outweighs striking now."
            )

        return RYDEDecision(
            action=action,
            confidence_score=round(strike_score, 2),
            net_savings=round(net_savings, 2),
            expected_future_gain=round(expected_future_gain, 2),
            probability_of_future_drop=round(prob_drop * 100, 2),
            seat_urgency_multiplier=round(urgency, 3),
            reasoning=reasoning,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _drop_probability(self, days: float, volatility: float) -> float:
        """
        Probability that the price will DROP further from its current level,
        shaped to match the empirical airline U-curve:

          Far out (>90d)  : 0.60 – 0.90  (airlines start high, still falling)
          55 – 90d        : 0.20 – 0.60  (descending toward the sweet spot)
          30 – 55d        : 0.08 – 0.20  (sweet spot valley — price is near floor)
          14 – 30d        : 0.05 – 0.15  (post-sweet-spot, slow creep up begins)
          7  – 14d        : 0.00 – 0.05  (last-minute spike territory)
          0  –  7d        : 0.00          (prices only rise this close in)

        volatility shifts the curve: >1 widens uncertainty, <1 narrows it.
        """
        if days <= 0:
            return 0.0

        vol = max(0.1, min(3.0, volatility))

        if days < 7:
            return 0.0

        if days < 14:
            base = 0.05 * (days - 7) / 7
            return min(0.95, base * (0.7 + 0.3 * vol))

        if days < 30:
            t = (days - 14) / 16   # 0 at day 14, 1 at day 30
            base = 0.05 + 0.10 * t
            return min(0.95, base * (0.8 + 0.2 * vol))

        if days <= 55:
            # Gaussian valley: probability is lowest at the sweet spot (~42d)
            center = 42.0
            sigma = max(5.0, 10.0 - 2.0 * (vol - 1))  # volatile = narrower window
            gaussian = math.exp(-((days - center) ** 2) / (2 * sigma ** 2))
            prob = 0.20 - 0.12 * gaussian
            return max(0.0, min(0.95, prob))

        if days <= 90:
            t = (days - 55) / 35   # 0 at 55d, 1 at 90d
            base = 0.20 + 0.40 * t
            return min(0.95, base + 0.05 * (vol - 1))

        # Far out: sigmoid rising toward 0.90 max
        extra = math.tanh((days - 90) / (40 / vol))
        return min(0.90, 0.60 + 0.30 * extra)

    def _seat_urgency(self, seats_remaining: int) -> float:
        """
        Amplifies the strike score when inventory is scarce.
        Capped at 2.5x so low-seat counts don't override bad economics.

          1 seat  → ~2.5x
          3 seats → ~1.6x
          9 seats → ~1.1x
          30+     → ~1.0x
        """
        if seats_remaining <= 0:
            return 2.5
        return min(2.5, 1.0 + 1.5 * math.exp(-seats_remaining / 4))
