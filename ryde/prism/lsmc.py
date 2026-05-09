"""
Longstaff-Schwartz Monte Carlo Optimal Stopping for flight rebooking.

Paper: Longstaff & Schwartz (2001) "Valuing American Options by Simulation:
A Simple Least-Squares Approach", Review of Financial Studies 14(1).

Analogy:
  American option exercise  ↔  rebooking decision
  Intrinsic value           ↔  net_savings (price drop − cancellation fee)
  Continuation value        ↔  expected savings from waiting
  Stopping rule             ↔  rebook when intrinsic ≥ continuation
"""

from typing import Tuple

import numpy as np


class LSMCOptimalStopper:
    """Backward induction with least-squares regression."""

    def _basis(self, x: np.ndarray) -> np.ndarray:
        """Normalized polynomial basis [1, z, z², z³]."""
        lo, hi = x.min(), x.max()
        z = np.zeros_like(x) if hi == lo else (x - lo) / (hi - lo)
        return np.column_stack([np.ones_like(z), z, z ** 2, z ** 3])

    def compute(
        self,
        price_paths: np.ndarray,
        original_price: float,
        cancellation_fee: float = 0.0,
    ) -> Tuple[float, np.ndarray]:
        """
        Run LSMC backward induction.

        Parameters
        ----------
        price_paths : ndarray (n_paths, n_steps+1)
        original_price : float
        cancellation_fee : float

        Returns
        -------
        expected_value : float
            Mean optimal savings across all paths.
        exercise_boundary : ndarray (n_steps+1,)
            Median price at each step where early exercise is optimal.
        """
        n_paths, n_steps_plus_1 = price_paths.shape
        n_steps = n_steps_plus_1 - 1

        # Cashflow matrix: entry [i, t] = payoff if path i exercises at step t
        cashflows = np.zeros((n_paths, n_steps_plus_1), dtype=float)

        # Terminal: must decide on final day
        cashflows[:, -1] = np.maximum(
            original_price - price_paths[:, -1] - cancellation_fee, 0.0
        )

        boundary = np.full(n_steps_plus_1, np.nan)

        # Backward induction
        for t in range(n_steps - 1, 0, -1):
            prices_t = price_paths[:, t]
            intrinsic = original_price - prices_t - cancellation_fee
            itm = intrinsic > 0  # in-the-money paths

            if itm.sum() < 4:
                continue

            # Continuation value: sum of all future cashflows for ITM paths
            continuation = cashflows[itm, t + 1:].sum(axis=1)

            # Regress continuation against basis functions of current price
            X = self._basis(prices_t[itm])
            try:
                coeffs, _, _, _ = np.linalg.lstsq(X, continuation, rcond=None)
                estimated_cv = X @ coeffs
            except np.linalg.LinAlgError:
                continue

            # Exercise where intrinsic beats estimated continuation
            exercise = intrinsic[itm] >= np.maximum(estimated_cv, 0.0)
            idx = np.where(itm)[0][exercise]

            if len(idx) > 0:
                boundary[t] = float(np.median(prices_t[idx]))
                cashflows[idx, t] = intrinsic[idx]
                cashflows[idx, t + 1:] = 0.0  # clear future cashflows for exercised paths

        expected_value = float(cashflows.max(axis=1).mean())
        return expected_value, boundary
