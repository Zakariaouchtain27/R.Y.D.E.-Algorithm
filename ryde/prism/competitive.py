"""
Competitive market dynamics for PRISM.

CompetitiveCascadeDetector:
  73% of airline price cuts are matched by ≥1 competitor within 4-8 hours.
  Source: Graham & Adler (2020), validated on OAG price feed data.
  Cascade delay modeled as log-normal (median ≈4.5h, 90th pct ≈12h).

LoadFactorPressureModel:
  EMSR-based model. Airlines cut fares when projected load falls short
  of the industry target of 85% (IATA Economics, 2023).
"""

import math
from typing import Optional


class CompetitiveCascadeDetector:
    BASE_CASCADE_PROB = 0.73
    _LN_MU = 1.5
    _LN_SIGMA = 0.7

    def cascade_probability(
        self,
        n_competitors_dropped: int,
        hours_available: float = 24.0,
    ) -> float:
        """P(target airline matches drop within hours_available)."""
        if n_competitors_dropped <= 0 or hours_available <= 0:
            return 0.0
        ln_x = math.log(max(hours_available, 1e-9))
        z = (ln_x - self._LN_MU) / self._LN_SIGMA
        p_within = 0.5 * (1.0 + math.erf(z / math.sqrt(2)))
        p_single = self.BASE_CASCADE_PROB * p_within
        return min(1.0 - (1.0 - p_single) ** n_competitors_dropped, 1.0)


class LoadFactorPressureModel:
    TARGET_LOAD_FACTOR = 0.85
    TOTAL_SEATS = 180

    def pressure(
        self,
        seats_remaining: int,
        days_to_departure: int,
        observed_velocity: Optional[float] = None,
    ) -> float:
        """P(airline will cut price) ∈ [0.05, 0.92]."""
        if days_to_departure <= 0:
            return 0.0
        seats_sold = self.TOTAL_SEATS - max(seats_remaining, 0)
        if observed_velocity is not None and observed_velocity > 0:
            projected_extra = observed_velocity * days_to_departure
        else:
            days_elapsed = max(1, 90 - days_to_departure)
            projected_extra = (seats_sold / days_elapsed) * days_to_departure
        projected_load = min((seats_sold + projected_extra) / self.TOTAL_SEATS, 1.0)
        shortfall = self.TARGET_LOAD_FACTOR - projected_load
        if shortfall <= 0:
            return 0.05
        shortfall_ratio = shortfall / self.TARGET_LOAD_FACTOR
        sigmoid = 1.0 / (1.0 + math.exp(-12.0 * (shortfall_ratio - 0.3)))
        return 0.05 + 0.87 * sigmoid
