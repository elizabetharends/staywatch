"""Runs the analytics on the gold views and saves the README charts to docs/images/.

Usage: uv run python scripts/forecast.py

1. Forecast of bookings per import (ETS) with a 95% prediction interval, backtested
   against a naive "same as last import" forecast.
2. Regression of nightly rate on booking window.
3. Bookings per import with the z-score band, held imports marked.
Every result is written to stats_log with its formula and inputs.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import stats  # noqa: E402
from agent.db import Database  # noqa: E402
from agent.settings import ROOT, load_settings, resolve  # noqa: E402

IMAGES = ROOT / "docs" / "images"
INK, INK_2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#fcfcfb"
SERIES_1, SERIES_2, CRITICAL = "#2a78d6", "#eb6834", "#d03b3b"


def style(ax, title, xlabel, ylabel):
    ax.set_facecolor(SURFACE)
    ax.figure.set_facecolor(SURFACE)
    ax.set_title(title, loc="left", color=INK, fontsize=12, fontweight="bold", pad=12)
    ax.set_xlabel(xlabel, color=INK_2)
    ax.set_ylabel(ylabel, color=INK_2)
    ax.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=MUTED)


def chart_forecast(y: np.ndarray, f: dict, path: Path) -> None:
    n, h, p = len(y), len(f["path"]["mean"]), f["path"]
    fig, ax = plt.subplots(figsize=(10, 4.5))
    x_hist, x_fut = np.arange(n), np.arange(n, n + h)
    ax.plot(x_hist, y, color=INK_2, linewidth=1.5, label="Actual bookings per import")
    ax.fill_between(x_fut, p["lower"], p["upper"], color=SERIES_1, alpha=0.15, linewidth=0,
                    label="95% prediction interval")
    ax.plot(x_fut, p["mean"], color=SERIES_1, linewidth=2, label="ETS forecast")
    x_test = np.arange(n - p["test_n"], n)
    ax.plot(x_test, p["backtest_pred"], color=SERIES_1, linewidth=2, linestyle="--",
            label=f"Backtest: ETS (MAE {f['inputs']['MAE']:.2f})")
    ax.plot(x_test, p["backtest_naive"], color=SERIES_2, linewidth=2, linestyle=":",
            label=f"Backtest: same as last import (MAE {f['inputs']['MAE_naive']:.2f})")
    ax.axvline(n - p["test_n"] - 0.5, color=MUTED, linewidth=1, linestyle="-")
    ax.text(n - p["test_n"] - 0.3, ax.get_ylim()[1], " held out", color=MUTED, va="top", fontsize=9)
    style(ax, "Bookings per import: forecast with 95% prediction interval", "Import number (published and released)",
          "Bookings")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper left", ncol=2)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_regression(x: list, y: list, r: dict, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.scatter(x, y, s=28, color=SERIES_1, alpha=0.55, edgecolor=SURFACE, linewidth=0.8, label="Booking")
    xs = np.linspace(min(x), max(x), 50)
    b0, b1 = r["inputs"][r"\beta_0"], r["inputs"][r"\beta_1"]
    ax.plot(xs, b0 + b1 * xs, color=INK, linewidth=2,
            label=f"Fit: rate = {b0:.1f} {'-' if b1 < 0 else '+'} {abs(b1):.2f} x days")
    style(ax, f"Nightly rate vs. booking window (r = {r['inputs']['r']:.2f}, n = {r['inputs']['n']})",
          "Booking window (days before check-in)", "Average daily rate (USD)")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def chart_trigger(rows: list, threshold: float, path: Path) -> None:
    """Band = the exact baseline each decision used (from stats_log), not one overall mean."""
    counts = np.array([r["bookings_count"] for r in rows], dtype=float)
    held = np.array([r["status"] in ("held", "released", "rejected") for r in rows])
    lower = np.array([r["mean"] - threshold * r["s"] if r["s"] is not None else np.nan for r in rows])
    upper = np.array([r["mean"] + threshold * r["s"] if r["s"] is not None else np.nan for r in rows])
    x = np.arange(len(counts))
    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.fill_between(x, lower, upper, color=SERIES_1, alpha=0.15, linewidth=0, step="mid",
                    label=f"Normal range per decision: x̄ ± {threshold:g} s")
    ax.plot(x, counts, color=INK_2, linewidth=1.2, marker="o", markersize=4, label="Bookings per import")
    ax.scatter(x[held], counts[held], s=90, marker="X", color=CRITICAL, edgecolor=SURFACE, linewidth=1.2,
               zorder=3, label="Held by the trigger")
    untested = np.isnan(lower)
    if untested.any():
        ax.axvspan(-0.5, x[untested].max() + 0.5, color=GRID, alpha=0.5, linewidth=0)
        ax.text(x[untested].max() / 2, ax.get_ylim()[1], "warm-up:\nno test", ha="center", va="top",
                fontsize=8, color=MUTED)
    style(ax, "Anomaly trigger: bookings per import against the z-score band", "Import number (all imports)",
          "Bookings")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK_2, loc="upper center", bbox_to_anchor=(0.5, -0.16), ncol=3)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def main() -> None:
    settings = load_settings()
    IMAGES.mkdir(parents=True, exist_ok=True)
    db = Database(resolve(settings["paths"]["sqlite"]))
    db.run_id = f"analysis_{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    db.file_name = None

    series = [r["bookings_count"] for r in db.run_sql(
        "analyze", "SELECT bookings_count FROM gold_import_metrics ORDER BY import_seq")]
    f = stats.forecast(series, horizon=10, season=30)
    db.log_stats("analyze", f)
    print("Forecast:", f["interpretation"])
    if f["path"]:
        chart_forecast(np.array(series, dtype=float), f, IMAGES / "forecast.png")

    pairs = db.run_sql("analyze", "SELECT booking_window_days AS x, average_daily_rate_usd AS y FROM gold_bookings"
                                  " WHERE booking_window_days IS NOT NULL AND average_daily_rate_usd IS NOT NULL")
    r = stats.regression([p["x"] for p in pairs], [p["y"] for p in pairs])
    db.log_stats("analyze", r)
    print("Regression:", r["interpretation"])
    if r["result"] is not None:
        chart_regression([p["x"] for p in pairs], [p["y"] for p in pairs], r, IMAGES / "regression.png")

    rows = []
    for r in db.run_sql("analyze",
                        "SELECT i.bookings_count, i.status, s.inputs_json FROM imports i"
                        " JOIN stats_log s ON s.run_id = i.run_id AND s.step = 'decide'"
                        " WHERE i.status != 'loading' ORDER BY i.rowid"):
        inputs = json.loads(r["inputs_json"])
        rows.append({"bookings_count": r["bookings_count"], "status": r["status"],
                     "mean": inputs.get(r"\bar{x}"), "s": inputs.get("s")})
    if len(rows) >= stats.MIN_HISTORY:
        chart_trigger(rows, settings["decide"]["z_threshold"], IMAGES / "trigger.png")

    (ROOT / "docs" / "analysis.json").write_text(json.dumps(
        {"forecast": {k: v for k, v in f.items() if k != "path"}, "regression": r}, indent=2, default=str))
    db.close()
    print("Charts saved to docs/images/; results in docs/analysis.json and stats_log")


if __name__ == "__main__":
    main()
