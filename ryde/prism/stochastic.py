"""
Ornstein-Uhlenbeck stochastic price model.

    dP = κ(θ(t) − P)dt + σdW

Fitted via OLS on the discretized process:
    ΔP_t = a + b·P_t + ε  →  κ = −b,  σ = std(ε)
"""

import math
from typing import List, Optional

import numpy as np


class OrnsteinUhlenbeck:
    """Fit once, simulate many times."""

    _KAPPA_MIN, _KAPPA_MAX = 1e-4, 2.0
    _SIGMA_MIN, _SIGMA_MAX = 1.0, 200.0      # USD / day
    _VOL_MIN,   _VOL_MAX   = 0.3, 3.0        # volatility_multiplier bounds

    def __init__(self):
        self.kappa: float = 0.05   # slow reversion default
        self.sigma: float = 15.0   # $15/day jitter default

    def fit(self, price_series: List[float]) -> "OrnsteinUhlenbeck":
        """
        Fit κ and σ from a chronological price series via OLS.
        Returns self — chainable.
        """
        if len(price_series) < 10:
            return self

        prices = np.array(price_series, dtype=float)
        P  = prices[:-1]
        dP = np.diff(prices)

        X = np.column_stack([np.ones_like(P), P])
        try:
            coeffs, _, _, _ = np.linalg.lstsq(X, dP, rcond=None)
        except np.linalg.LinAlgError:
            return self

        a, b = coeffs
        self.kappa = float(np.clip(-b, self._KAPPA_MIN, self._KAPPA_MAX))
        self.sigma = float(np.clip(np.std(dP - (a + b * P)), self._SIGMA_MIN, self._SIGMA_MAX))
        return self

    @staticmethod
    def u_curve_mean(reference_price: float, days_to_dep: float) -> float:
        """
        Airline pricing U-curve θ(t) — fully continuous, zero discontinuities.

        Key values (multiples of reference_price):
          d = 0    → ×1.20  last-minute spike peak
          d = 7    → smooth junction (∼×1.02), both branches agree exactly
          d = 42   → ×0.87  advance-purchase sweet spot (−13%)
          d = 90   → ×1.045 far-out premium (≈5%)
          d → ∞    → ×1.05  long-run premium floor

        Implementation
        --------------
        For d ≥ 7:
            factor = BASE - DIP_DEPTH * exp(-0.5 * ((d - 42) / 18)^2)
        For d < 7:
            linear from SPIKE_PEAK at d=0 down to the main-curve value at d=7,
            so the two pieces meet without a jump.
        """
        d = max(days_to_dep, 0.0)

        SWEET_SPOT = 42.0
        WIDTH      = 18.0
        DIP_DEPTH  = 0.18
        BASE       = 1.05   # far-out premium level
        SPIKE_PEAK = 1.20   # last-minute peak

        gaussian = math.exp(-0.5 * ((d - SWEET_SPOT) / WIDTH) ** 2)

        if d >= 7:
            factor = BASE - DIP_DEPTH * gaussian
        else:
            # Compute the main-curve value at d=7 so both branches meet exactly
            g7       = math.exp(-0.5 * ((7.0 - SWEET_SPOT) / WIDTH) ** 2)
            d7_value = BASE - DIP_DEPTH * g7
            # Linear ramp: SPIKE_PEAK at d=0, d7_value at d=7
            factor = SPIKE_PEAK - (d / 7.0) * (SPIKE_PEAK - d7_value)

        return max(reference_price * factor, 1.0)

    def simulate_paths(
        self,
        current_price: float,
        reference_price: float,
        days: int,
        n_paths: int = 5000,
        rng: Optional[np.random.Generator] = None,
        volatility_multiplier: float = 1.0,
    ) -> np.ndarray:
        """
        Euler-Maruyama discretization.
        Returns ndarray (n_paths, days+1), prices floored at $1.

        volatility_multiplier is clamped to [0.3, 3.0] before use —
        passing extreme values (e.g. 10) would produce nonsensical paths.
        """
        if rng is None:
            rng = np.random.default_rng()

        vol_mult  = float(np.clip(volatility_multiplier, self._VOL_MIN, self._VOL_MAX))
        sigma_eff = self.sigma * vol_mult

        paths = np.empty((n_paths, days + 1), dtype=float)
        paths[:, 0] = current_price

        # Pre-compute θ(t): days remaining decreases as we step forward
        theta = np.array(
            [self.u_curve_mean(reference_price, days - t) for t in range(days + 1)],
            dtype=float,
        )
        noise = rng.standard_normal((n_paths, days))

        for t in range(days):
            drift     = self.kappa * (theta[t] - paths[:, t])  # dt = 1 day
            diffusion = sigma_eff * noise[:, t]                 # sqrt(dt) = 1
            paths[:, t + 1] = np.maximum(paths[:, t] + drift + diffusion, 1.0)

        return paths
