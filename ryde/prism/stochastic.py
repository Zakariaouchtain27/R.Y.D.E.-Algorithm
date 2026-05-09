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
    _SIGMA_MIN, _SIGMA_MAX = 1.0, 200.0   # USD / day

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
        P = prices[:-1]
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
        Airline U-curve long-run mean θ(t):
          >90d   → +5%  (early booking premium)
          ~42d   → −15% (advance purchase sweet spot)
          <7d    → +20% (last-minute spike)
        """
        d = max(days_to_dep, 0.0)
        if d <= 7:
            factor = 1.20 - (d / 7) * 0.10
        elif d <= 90:
            center, width = 42.0, 18.0
            dip = -0.15 * math.exp(-0.5 * ((d - center) / width) ** 2)
            blend = 1.0 - (d - 7) / (90 - 7)
            factor = 1.0 + dip * blend + 0.02 * (1 - blend)
        else:
            factor = 1.05
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
        """
        if rng is None:
            rng = np.random.default_rng()

        sigma_eff = self.sigma * volatility_multiplier
        paths = np.empty((n_paths, days + 1), dtype=float)
        paths[:, 0] = current_price

        # Pre-compute θ(t): time decreasing to departure as we step forward
        theta = np.array(
            [self.u_curve_mean(reference_price, days - t) for t in range(days + 1)],
            dtype=float,
        )
        noise = rng.standard_normal((n_paths, days))

        sqrt_dt = math.sqrt(1.0)  # dt = 1 day
        for t in range(days):
            drift = self.kappa * (theta[t] - paths[:, t])
            diffusion = sigma_eff * sqrt_dt * noise[:, t]
            paths[:, t + 1] = np.maximum(paths[:, t] + drift + diffusion, 1.0)

        return paths
