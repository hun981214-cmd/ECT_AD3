#!/usr/bin/env python3
"""Temperature response characterization from a wide-range temp sweep.

Uses a cooling (or heating) transient with ambient logging to answer:

1. Sensitivity: how much does the complex ratio move per degree C
   (magnitude %/C, phase deg/C, and the raw_x/raw_y coefficients that an
   env correction would use)?
2. Thermal lag: how far does the probe signal lead/lag the RN171 ambient
   reading? (scanned by shifting the temperature series and maximizing R2)
3. Linearity: does a quadratic temperature term add real explanatory power
   over the 7+ degree span?
4. Correction quality: residual drift after (a) static linear temp+humidity,
   (b) lag-compensated linear, (c) lag-compensated quadratic models.

Run ids are comma-separated watchdog segments, stitched by timestamp.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

ANALYSIS_DIR = Path(__file__).resolve().parent
LOG_DIR = ANALYSIS_DIR.parent / "logs"

SURFACE = "#fcfcfb"
INK = "#1f1f1e"
INK_2 = "#5f5e57"
GRID = "#e6e5e0"
C_1 = "#2a78d6"
C_2 = "#1baf7a"
C_TEMP = "#eda100"
C_NEUTRAL = "#8a897f"


def load_stitched(run_arg: str, log_dir: Path) -> pd.DataFrame:
    frames = []
    for rid in [r.strip() for r in run_arg.split(",") if r.strip()]:
        path = Path(rid)
        if not path.is_file():
            path = log_dir / f"ad3_log_{rid}.csv"
        frames.append(pd.read_csv(path))
    df = pd.concat(frames, ignore_index=True)
    ts = pd.to_datetime(df["timestamp"])
    df = df.assign(time_s=(ts - ts.iloc[0]).dt.total_seconds()).sort_values("time_s")
    ok = df[df["row_status"] == "ok"].dropna(subset=["raw_x", "raw_y", "temp_c", "humid_pct"])
    return ok.reset_index(drop=True)


def fit_r2(y: np.ndarray, X: np.ndarray) -> tuple[float, np.ndarray, np.ndarray]:
    A = np.column_stack([np.ones(len(y)), X])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    resid = y - A @ coef
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    r2 = 1.0 - float(np.sum(resid**2)) / ss_tot if ss_tot > 0 else float("nan")
    return r2, coef, resid


def shift_series(v: np.ndarray, t_s: np.ndarray, lag_s: float) -> np.ndarray:
    """Value of v at time (t - lag_s), i.e. positive lag means the signal
    responds to the environment value from lag_s seconds ago."""
    return np.interp(t_s - lag_s, t_s, v)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="comma-separated run ids or CSV paths")
    parser.add_argument("--log-dir", default=str(LOG_DIR))
    parser.add_argument("--skip-first-min", type=float, default=5.0)
    parser.add_argument("--max-lag-min", type=float, default=15.0)
    parser.add_argument("--out-prefix", default=str(ANALYSIS_DIR / "temp_response"))
    args = parser.parse_args()

    ok = load_stitched(args.run_id, Path(args.log_dir))
    t_s = ok["time_s"].to_numpy(dtype=float)
    keep = t_s >= t_s[0] + args.skip_first_min * 60.0
    ok, t_s = ok[keep].reset_index(drop=True), t_s[keep]

    x = ok["raw_x"].to_numpy(dtype=float)
    y = ok["raw_y"].to_numpy(dtype=float)
    mag = np.hypot(x, y)
    phase = np.degrees(np.unwrap(np.radians(ok["phase_deg"].to_numpy(dtype=float))))
    temp = ok["temp_c"].to_numpy(dtype=float)
    humid = ok["humid_pct"].to_numpy(dtype=float)

    span = temp.max() - temp.min()
    print(f"rows={len(ok)}  duration={(t_s[-1]-t_s[0])/3600:.2f} h  "
          f"temp {temp.min():.2f}..{temp.max():.2f} C (span {span:.2f})  "
          f"humid {humid.min():.1f}..{humid.max():.1f} %")

    # -- thermal lag: two-sided scan maximizing R2 of mag ~ temp -------------
    # Negative lag = the probe signal LEADS the RN171 reading (the bare coil
    # responds to air faster than the boxed sensor) - this is the observed
    # regime on this setup, so the scan must include negative lags.
    lags = np.arange(-args.max_lag_min * 60.0, args.max_lag_min * 60.0 + 1, 15.0)
    lag_r2 = []
    for lag in lags:
        temp_l = shift_series(temp, t_s, lag)
        r2, _, _ = fit_r2(mag, temp_l.reshape(-1, 1))
        lag_r2.append(r2)
    lag_r2 = np.array(lag_r2)
    best_lag_s = float(lags[int(np.argmax(lag_r2))])
    zero_idx = int(np.argmin(np.abs(lags)))
    print(f"thermal lag: best {best_lag_s/60:+.1f} min "
          f"(negative = signal leads RN171; R2 {lag_r2.max():.4f} vs "
          f"{lag_r2[zero_idx]:.4f} at zero lag)")
    if abs(best_lag_s) >= args.max_lag_min * 60.0 - 16:
        print("[caution] lag optimum sits at the scan edge; widen --max-lag-min")

    temp_lag = shift_series(temp, t_s, best_lag_s)
    humid_lag = shift_series(humid, t_s, best_lag_s)

    # -- models --------------------------------------------------------------
    results = {}
    targets = {"magnitude": mag, "phase_deg": phase, "raw_x": x, "raw_y": y}
    for name, target in targets.items():
        r2_a, coef_a, res_a = fit_r2(target, np.column_stack([temp, humid]))
        r2_b, coef_b, res_b = fit_r2(target, np.column_stack([temp_lag, humid_lag]))
        r2_c, coef_c, res_c = fit_r2(
            target, np.column_stack([temp_lag, temp_lag**2, humid_lag])
        )
        scale = abs(np.mean(target)) if name == "magnitude" else 1.0
        results[name] = {
            "static_linear": {"r2": r2_a, "temp_coef": float(coef_a[1]), "humid_coef": float(coef_a[2]),
                              "resid_p95_p05": float(np.percentile(res_a, 95) - np.percentile(res_a, 5))},
            "lag_linear": {"r2": r2_b, "temp_coef": float(coef_b[1]), "humid_coef": float(coef_b[2]),
                           "resid_p95_p05": float(np.percentile(res_b, 95) - np.percentile(res_b, 5))},
            "lag_quadratic": {"r2": r2_c, "temp_coef": float(coef_c[1]), "temp2_coef": float(coef_c[2]),
                              "humid_coef": float(coef_c[3]),
                              "resid_p95_p05": float(np.percentile(res_c, 95) - np.percentile(res_c, 5))},
            "raw_p95_p05": float(np.percentile(target, 95) - np.percentile(target, 5)),
            "mean": float(np.mean(target)),
        }

    m = results["magnitude"]
    mag_mean = m["mean"]
    sens_pct_per_c = m["lag_linear"]["temp_coef"] / mag_mean * 100
    phase_sens = results["phase_deg"]["lag_linear"]["temp_coef"]
    print("\n=== sensitivity (lag-compensated linear, global) ===")
    print(f"|ratio|: {sens_pct_per_c:+.4f} %/C   phase: {phase_sens:+.5f} deg/C")
    print(f"raw_x: {results['raw_x']['lag_linear']['temp_coef']:+.3e} /C   "
          f"raw_y: {results['raw_y']['lag_linear']['temp_coef']:+.3e} /C")
    # Local slope of the quadratic model at the range ends: with a wide span
    # the response is measurably curved, so the global linear coefficient
    # under/over-corrects at the ends. Report both.
    q = m["lag_quadratic"]
    for t_ref, label in ((temp.min(), "cold end"), (temp.max(), "warm end")):
        local = (q["temp_coef"] + 2 * q["temp2_coef"] * t_ref) / mag_mean * 100
        print(f"local slope @{t_ref:.1f} C ({label}): {local:+.4f} %/C")
    print("\n=== model comparison on |ratio| (residual p95-p05, % of mean) ===")
    print(f"raw span          : {m['raw_p95_p05']/mag_mean*100:.4f} %")
    for key, label in (("static_linear", "static linear T+H"),
                       ("lag_linear", f"lag {best_lag_s/60:+.0f} min linear T+H"),
                       ("lag_quadratic", "lag quadratic T+T^2+H")):
        r = m[key]
        print(f"{label:22}: {r['resid_p95_p05']/mag_mean*100:.4f} %  (R2 {r['r2']:.4f})")
    quad_gain = m["lag_quadratic"]["r2"] - m["lag_linear"]["r2"]
    print(f"quadratic term adds dR2 = {quad_gain:.4f} on |ratio| "
          f"(local slope changes across the span are the better linearity test)")

    # Out-of-sample check: fit the lag-quadratic on the first half (time),
    # evaluate on the second half. In-sample residuals overstate deployed
    # correction quality when residuals are autocorrelated.
    half = len(mag) // 2
    X_full = np.column_stack([temp_lag, temp_lag**2, humid_lag])
    A = np.column_stack([np.ones(half), X_full[:half]])
    coef_h, *_ = np.linalg.lstsq(A, mag[:half], rcond=None)
    pred2 = np.column_stack([np.ones(len(mag) - half), X_full[half:]]) @ coef_h
    oos = mag[half:] - pred2
    oos_span = float(np.percentile(oos, 95) - np.percentile(oos, 5)) / mag_mean * 100
    print(f"out-of-sample (train 1st half, test 2nd half, lag-quadratic): "
          f"residual span {oos_span:.4f} %  mean offset {np.mean(oos)/mag_mean*100:+.4f} %")

    corr_th = float(np.corrcoef(temp, humid)[0, 1])
    print(f"temp-humidity correlation (global): {corr_th:+.2f} "
          f"[note: global correlation can hide regime collinearity; the "
          f"magnitude humidity coefficient is near zero regardless, while the "
          f"phase model does use humidity]")

    # -- figure ---------------------------------------------------------------
    fig = plt.figure(figsize=(14, 9), dpi=140)
    fig.patch.set_facecolor(SURFACE)
    gs = fig.add_gridspec(3, 2, width_ratios=[1.4, 1.0], hspace=0.5, wspace=0.28)
    t_h = (t_s - t_s[0]) / 3600.0

    def style(ax):
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.8)
        ax.tick_params(colors=INK_2, labelsize=8)
        for s in ax.spines.values():
            s.set_color(GRID)
        ax.xaxis.label.set_color(INK_2)
        ax.yaxis.label.set_color(INK_2)
        ax.title.set_color(INK)

    ax = fig.add_subplot(gs[0, 0])
    style(ax)
    ax.plot(t_h, (mag / mag_mean - 1) * 100, color=C_1, linewidth=1.2)
    ax.set_ylabel("|ratio| dev (%)")
    ax.set_title("signal and temperature during the sweep")

    ax = fig.add_subplot(gs[1, 0])
    style(ax)
    ax.plot(t_h, temp, color=C_TEMP, linewidth=1.2)
    ax.set_ylabel("temp_c")

    ax = fig.add_subplot(gs[2, 0])
    style(ax)
    ax.plot(t_h, humid, color="#4a3aa7", linewidth=1.2)
    ax.set_ylabel("humid_pct")
    ax.set_xlabel("elapsed (h)")

    ax = fig.add_subplot(gs[0:2, 1])
    style(ax)
    sc = ax.scatter(temp_lag, (mag / mag_mean - 1) * 100, s=5, c=t_h, cmap="viridis", alpha=0.6)
    order = np.argsort(temp_lag)
    _, coef_q, _ = fit_r2(mag, np.column_stack([temp_lag, temp_lag**2, humid_lag]))
    fit_line = (coef_q[0] + coef_q[1] * temp_lag[order] + coef_q[2] * temp_lag[order] ** 2
                + coef_q[3] * float(np.mean(humid_lag)))
    ax.plot(temp_lag[order], (fit_line / mag_mean - 1) * 100, color=INK, linewidth=1.6,
            linestyle=(0, (4, 3)))
    cbar = fig.colorbar(sc, ax=ax)
    cbar.set_label("elapsed (h)", fontsize=8)
    ax.set_xlabel(f"temp_c shifted {best_lag_s/60:.0f} min")
    ax.set_ylabel("|ratio| dev (%)")
    ax.set_title(f"characteristic: {sens_pct_per_c:+.3f} %/C (lag-comp.)")

    ax = fig.add_subplot(gs[2, 1])
    style(ax)
    _, _, res_lin = fit_r2(mag, np.column_stack([temp_lag, humid_lag]))
    _, _, res_quad = fit_r2(mag, np.column_stack([temp_lag, temp_lag**2, humid_lag]))
    ax.plot(t_h, res_lin / mag_mean * 100, color=C_2, linewidth=1.0)
    ax.plot(t_h, res_quad / mag_mean * 100, color=C_NEUTRAL, linewidth=1.0)
    ax.annotate("lag linear", (t_h[-1], (res_lin / mag_mean * 100)[-1]),
                textcoords="offset points", xytext=(6, 6), fontsize=8, color=C_2, fontweight="bold")
    ax.annotate("lag quadratic", (t_h[-1], (res_quad / mag_mean * 100)[-1]),
                textcoords="offset points", xytext=(6, -12), fontsize=8, color=C_NEUTRAL, fontweight="bold")
    ax.set_ylabel("residual (%)")
    ax.set_xlabel("elapsed (h)")
    ax.set_title("correction residuals")

    fig.suptitle(f"AD3 ratio probe temperature response - {span:.1f} C sweep "
                 f"({Path(args.run_id.split(',')[0]).name})", color=INK, fontsize=12)
    out_png = f"{args.out_prefix}.png"
    fig.savefig(out_png, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)

    summary = {
        "runs": args.run_id,
        "rows": int(len(ok)),
        "temp_span_c": span,
        "best_lag_min": best_lag_s / 60.0,
        "sensitivity_mag_pct_per_c": sens_pct_per_c,
        "sensitivity_phase_deg_per_c": phase_sens,
        "temp_humid_corr": corr_th,
        "results": results,
    }
    out_json = f"{args.out_prefix}.json"
    Path(out_json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nwrote {out_png}")
    print(f"wrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
