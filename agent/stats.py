"""Statistical methods. Each returns the same shape, so the dashboard can show the formula,
the values plugged in and a plain-English interpretation, and stats_log keeps them all:

    {"method", "formula_latex", "inputs", "result", "threshold", "interpretation"}
"""
import math
import warnings

import numpy as np
from scipy import stats as sps

MIN_HISTORY = 10      # imports needed before the z-score is trusted
S_FLOOR = 0.5         # avoids dividing by ~0 when recent imports were all identical


def zscore(x: float, history: list[float], threshold: float = 3.0) -> dict:
    """Anomaly trigger: how many standard deviations this import is from recent imports."""
    n = len(history)
    if n < MIN_HISTORY:
        return {"method": "z-score", "formula_latex": r"z = \frac{x - \bar{x}}{s}",
                "inputs": {"x": x, "n": n}, "result": None, "threshold": threshold,
                "interpretation": f"Only {n} earlier imports (need {MIN_HISTORY}); published without a test."}
    mean = float(np.mean(history))
    s_raw = float(np.std(history, ddof=1))
    s = max(s_raw, S_FLOOR)
    z = (x - mean) / s
    direction = "above" if z > 0 else "below"
    verdict = "Unusual: held for a person to review." if abs(z) > threshold else "Within the normal range."
    return {
        "method": "z-score", "formula_latex": r"z = \frac{x - \bar{x}}{s}",
        "inputs": {"x": x, r"\bar{x}": round(mean, 3), "s": round(s, 3), "n": n,
                   **({"s_before_floor": round(s_raw, 3)} if s != s_raw else {})},
        "result": round(z, 3), "threshold": threshold,
        "interpretation": f"This import has {x:g} bookings, {abs(z):.1f} standard deviations {direction} "
                          f"the average of the last {n} published imports ({mean:.1f}). {verdict}",
    }


def regression(x: list[float], y: list[float], x_name: str = "booking window (days)",
               y_name: str = "nightly rate (USD)") -> dict:
    """Linear regression of y on x, with Pearson r, R², p-value and a 95% CI for the slope."""
    n = len(x)
    formula = (r"r = \frac{\sum (x_i - \bar{x})(y_i - \bar{y})}{\sqrt{\sum (x_i - \bar{x})^2 "
               r"\sum (y_i - \bar{y})^2}}, \qquad \hat{y} = \beta_0 + \beta_1 x")
    if n < MIN_HISTORY:
        return {"method": "linear regression", "formula_latex": formula, "inputs": {"n": n},
                "result": None, "threshold": 0.05,
                "interpretation": f"Only {n} bookings with both values; need {MIN_HISTORY}."}
    fit = sps.linregress(x, y)
    t = sps.t.ppf(0.975, n - 2)
    lo, hi = fit.slope - t * fit.stderr, fit.slope + t * fit.stderr
    significant = fit.pvalue < 0.05
    per_week = fit.slope * 7
    if significant:
        meaning = (f"Each extra day booked ahead goes with a {abs(fit.slope):.2f} USD "
                   f"{'lower' if fit.slope < 0 else 'higher'} nightly rate ({abs(per_week):.2f} USD per week). "
                   f"The booking window explains {fit.rvalue ** 2:.0%} of the variation in rate.")
    else:
        meaning = "No reliable link between booking window and nightly rate in these bookings."
    return {
        "method": "linear regression", "formula_latex": formula,
        "inputs": {"n": n, r"\beta_0": round(fit.intercept, 3), r"\beta_1": round(fit.slope, 4),
                   "r": round(fit.rvalue, 3), "R^2": round(fit.rvalue ** 2, 3), "p": float(f"{fit.pvalue:.3g}"),
                   r"\beta_1\ 95\%\ CI": [round(lo, 4), round(hi, 4)], "x": x_name, "y": y_name},
        "result": round(fit.slope, 4), "threshold": 0.05,
        "interpretation": f"{meaning} (r = {fit.rvalue:.2f}, p = {fit.pvalue:.2g}, n = {n}; "
                          f"slope 95% CI {lo:.2f} to {hi:.2f}.) Correlation, not proof of cause: "
                          f"listings and seasons also differ.",
    }


def _ets_fit(series: np.ndarray, season: int):
    import pandas as pd
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel
    seasonal = len(series) >= 2 * season
    model = ETSModel(pd.Series(series, dtype=float), error="add", trend="add", damped_trend=True,
                     seasonal="add" if seasonal else None, seasonal_periods=season if seasonal else None)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # convergence chatter on short demo series
        return model.fit(disp=False), seasonal


def forecast(series: list[float], horizon: int = 10, season: int = 30) -> dict:
    """Exponential smoothing (ETS) forecast with a 95% prediction interval, backtested
    against a naive 'same as last import' forecast on held-out data."""
    y = np.asarray(series, dtype=float)
    n = len(y)
    formula = r"\hat{y}_{t+h} \pm 1.96\,\hat{\sigma}_h"
    if n < 20:
        return {"method": "ETS forecast", "formula_latex": formula, "inputs": {"n": n}, "result": None,
                "threshold": None, "interpretation": f"Only {n} imports; need 20 to forecast.", "path": None}

    fit, seasonal = _ets_fit(y, season)
    frame = fit.get_prediction(start=n, end=n + horizon - 1).summary_frame(alpha=0.05)
    mean, lower, upper = frame["mean"].to_numpy(), frame["pi_lower"].to_numpy(), frame["pi_upper"].to_numpy()
    sigma_1 = (upper[0] - mean[0]) / 1.96

    test_n = max(5, n // 5)
    train, test = y[:-test_n], y[-test_n:]
    back_fit, _ = _ets_fit(train, season)
    pred = np.asarray(back_fit.forecast(test_n), dtype=float)
    naive = np.repeat(train[-1], test_n)
    mae, mae_naive = float(np.mean(np.abs(test - pred))), float(np.mean(np.abs(test - naive)))
    nonzero = test != 0
    mape = float(100 * np.mean(np.abs((test[nonzero] - pred[nonzero]) / test[nonzero])))
    mape_naive = float(100 * np.mean(np.abs((test[nonzero] - naive[nonzero]) / test[nonzero])))
    better = mae < mae_naive
    return {
        "method": "ETS forecast", "formula_latex": formula,
        "inputs": {"n": n, "horizon": horizon, "seasonal_period": season if seasonal else None,
                   r"\hat{y}_{t+1}": round(float(mean[0]), 2), r"\hat{\sigma}_1": round(float(sigma_1), 2),
                   "interval_t+1": [round(float(lower[0]), 2), round(float(upper[0]), 2)],
                   "backtest_points": test_n, "MAE": round(mae, 3), "MAE_naive": round(mae_naive, 3),
                   "MAPE": round(mape, 1), "MAPE_naive": round(mape_naive, 1)},
        "result": round(float(mean[0]), 2), "threshold": None,
        "interpretation": (f"Next import: about {mean[0]:.1f} bookings (95% interval {lower[0]:.1f} to {upper[0]:.1f}). "
                           f"On the last {test_n} imports held out, the model's average error was {mae:.2f} bookings "
                           f"vs {mae_naive:.2f} for 'same as last import', so the model is "
                           f"{'better' if better else 'NOT better'} than the naive forecast."),
        "path": {"mean": mean.tolist(), "lower": lower.tolist(), "upper": upper.tolist(),
                 "backtest_pred": pred.tolist(), "backtest_naive": naive.tolist(), "test_n": test_n},
    }


def precision_recall(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else math.nan
    recall = tp / (tp + fn) if tp + fn else math.nan
    return {
        "method": "trigger precision and recall",
        "formula_latex": r"\mathrm{precision} = \frac{TP}{TP + FP}, \qquad \mathrm{recall} = \frac{TP}{TP + FN}",
        "inputs": {"TP": tp, "FP": fp, "FN": fn}, "result": round(precision, 3) if tp + fp else None,
        "threshold": None,
        "interpretation": f"Of {tp + fp} held imports, {tp} were real seeded anomalies (precision "
                          f"{precision:.0%}); of {tp + fn} seeded anomalies, {tp} were caught (recall {recall:.0%}).",
    }
