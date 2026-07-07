"""Unit tests for forecaster-sidecar ETS helpers."""

import unittest

import numpy as np

from server import _ets_forecast, _prediction_intervals


class TestETSForecast(unittest.TestCase):
    def test_ets_forecast_returns_intervals(self) -> None:
        rng = np.random.default_rng(42)
        values = (rng.random(30) * 10 + 50).tolist()
        ts, mean, lower, upper, err = _ets_forecast(values, horizon=6)
        self.assertEqual(err, "")
        self.assertEqual(len(ts), 6)
        self.assertEqual(len(mean), 6)
        self.assertEqual(len(lower), 6)
        self.assertEqual(len(upper), 6)
        for m, lo, hi in zip(mean, lower, upper):
            self.assertLessEqual(lo, m)
            self.assertLessEqual(m, hi)

    def test_ets_forecast_rejects_short_series(self) -> None:
        _, _, _, _, err = _ets_forecast([1.0, 2.0], horizon=3)
        self.assertIn("need >=", err)

    def test_prediction_intervals_via_summary_frame(self) -> None:
        import pandas as pd
        from statsmodels.tsa.api import ETSModel

        series = pd.Series(np.linspace(10.0, 20.0, 24))
        fit = ETSModel(series, error="add", trend="add").fit(maxiter=500, disp=False)
        result = fit.get_prediction(start=len(series), end=len(series) + 2)
        lower, upper = _prediction_intervals(result)
        self.assertEqual(len(lower), 3)
        self.assertEqual(len(upper), 3)


if __name__ == "__main__":
    unittest.main()
