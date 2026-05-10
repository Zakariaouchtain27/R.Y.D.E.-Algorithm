"""
Competitive market dynamics for PRISM.

CompetitiveCascadeDetector:
  Models the empirically observed tendency for airlines to match each
  other's price cuts. Cascade delay is log-normal (median ≈4.5h, 90th
  pct ≈12h), with independent cascade signals per competitor.

LoadFactorPressureModel:
  EMSR-based model. Airlines cut fares when projected load falls short
  of their target (IATA industry average: 85% load factor).
  total_seats is a parameter — pass the right value per aircraft type:
    narrowbody ≈ 180, widebody ≈ 300, regional ≈ 50.
"""

import math
from typing import Optional


class CompetitiveCascadeDetector:
    BASE_CASCADE_PROB = 0.73
    _LN_MU    = 1.5   # log-normal mean for cascade delay in hours
    _LN_SIGMA = 0.7   # log-normal sigma

    def cascade_probability(
        self,
        n_competitors_dropped: int,
        hours_available: float = 24.0,
    ) -> float:
        """
        P(target airline matches at least one competitor's drop
        within hours_available hours).
        """
        if n_competitors_dropped <= 0 or hours_available <= 0:
            return 0.0

        ln_x    = math.log(max(hours_available, 1e-9))
        z       = (ln_x - self._LN_MU) / self._LN_SIGMA
        p_within = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
        p_single = self.BASE_CASCADE_PROB * p_within

        # P(at least one of N independent cascades fires)
        return min(1.0 - (1.0 - p_single) ** n_competitors_dropped, 1.0)


class LoadFactorPressureModel:
    TARGET_LOAD_FACTOR   = 0.85
    DEFAULT_TOTAL_SEATS  = 180   # narrowbody default

    def pressure(
        self,
        seats_remaining: int,
        days_to_departure: int,
        observed_velocity: Optional[float] = None,
        total_seats: int = DEFAULT_TOTAL_SEATS,
    ) -> float:
        """
        P(airline will cut price) ∈ [0.05, 0.92].

        Parameters
        ----------
        seats_remaining  : seats not yet sold; clamped to [0, total_seats].
        days_to_departure: calendar days until departure.
        observed_velocity: seats sold per day (from booking velocity tracking).
        total_seats      : aircraft capacity — set per route, not per booking.
        """
        if days_to_departure <= 0:
            return 0.0

        # Some APIs return 999 or 0 as placeholders — clamp to valid range
        seats_remaining = max(0, min(seats_remaining, total_seats))
        seats_sold      = total_seats - seats_remaining

        if observed_velocity is not None and observed_velocity > 0:
            projected_extra = observed_velocity * days_to_departure
        else:
            # Extrapolate current pace over a 90-day booking window
            days_elapsed    = max(1, 90 - days_to_departure)
            projected_extra = (seats_sold / days_elapsed) * days_to_departure

        projected_load = min((seats_sold + projected_extra) / total_seats, 1.0)
        shortfall      = self.TARGET_LOAD_FACTOR - projected_load

        if shortfall <= 0:
            return 0.05  # on track — minimal pricing pressure

        shortfall_ratio = shortfall / self.TARGET_LOAD_FACTOR
        sigmoid = 1.0 / (1.0 + math.exp(-12.0 * (shortfall_ratio - 0.3)))
        return 0.05 + 0.87 * sigmoid
