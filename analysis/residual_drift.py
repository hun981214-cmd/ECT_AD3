#!/usr/bin/env python3
"""Residual drift of air (no-specimen) runs after environment correction.

Answers: once temperature/humidity are corrected, is there drift left, how
big is it, and what (if anything) does it correlate with?

Two correction flavors are evaluated:
- "deployed": the transported sweep model (analysis/temp_response_20260713.json
  lag-quadratic coefficients) applied blind, as the runtime would;
- a ladder of self-fitted models of increasing capability, whose best member
  bounds the *irreducible* residual: static linear T+H, static quadratic,
  lag-compensated quadratic (two-sided lag scan), first-order thermal filter
  (time-constant scan) quadratic, and the latter plus the AD3 PCB temperature.

Note on ad3_usb_v: it correlates with the residual (+0.3) but adversarial
verification (2026-07-14) showed it is a PROXY, not a cause - its slow part
tracks wall time (R2 0.56) and its fast part is an inverted low-pass room
thermometer (HVAC-band corr with temp_c -0.88); the fitted coefficient is
non-transportable (+99 vs +28 %/V between halves) and degrades forward
out-of-sample. Keep usb_v as a logged diagnostic; do NOT add it to runtime
corrections. The real lever for the remaining residual is a better thermal
dynamics model (the single-pole lag/RC family only attenuates the ~45 min
HVAC line 2-4.6x).

Residual diagnostics: total and slow-component spans (block means), linear
trend per 24 h with autocorrelation-adjusted uncertainty, block-mean scaling
(does averaging integrate down like noise or hit a drift floor), spectrum
around the ~45 min HVAC line, and correlations of the residual against every
logged channel. Split-half (fit first half, evaluate second) guards against
in-sample optimism.
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
TEMP_MODEL_PATH = ANALYSIS_DIR / "temp_response_20260713.json"

SURFACE = "#fcfcfb"
INK = "#1f1f1e"
INK_2 = "#5f5e57"
GRID = "#e6e5e0"
C_1 = "#2a78d6"
C_2 = "#1baf7a"
C_3 = "#eda100"
C_NEUTRAL = "#8a897f"

DRIFT24_RUNS = ("drift24_20260710_s01,drift24_20260710_s02,drift24_20260710_s03,"
                "drift24_20260710_s04,drift24_20260710_s05,drift24_20260710_s06")


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


def fit_r2(y: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    A = np.column_stack([np.ones(len(y)), X])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    return coef, y - A @ coef


def shift_series(v: np.ndarray, t_s: np.ndarray, lag_s: float) -> np.ndarray:
    return np.interp(t_s - lag_s, t_s, v)


def first_order_filter(v: np.ndarray, t_s: np.ndarray, tau_s: float) -> np.ndarray:
    """Causal first-order (RC) response of v with time constant tau."""
    if tau_s <= 0:
        return v.copy()
    out = np.empty_like(v)
    out[0] = v[0]
    for i in range(1, len(v)):
        dt = max(t_s[i] - t_s[i - 1], 1e-9)
        a = 1.0 - math.exp(-dt / tau_s)
        out[i] = out[i - 1] + a * (v[i] - out[i - 1])
    return out


def span(v: np.ndarray) -> float:
    return float(np.percentile(v, 95) - np.percentile(v, 5))


def block_means(v: np.ndarray, t_s: np.ndarray, block_s: float) -> np.ndarray:
    idx = ((t_s - t_s[0]) // block_s).astype(int)
    return np.array([v[idx == k].mean() for k in np.unique(idx) if np.sum(idx == k) >= 3])


def trend_per_24h(v: np.ndarray, t_s: np.ndarray) -> tuple[float, float]:
    """Linear slope per 24 h with autocorrelation-adjusted 95 % CI."""
    t = (t_s - t_s.mean()) / 86400.0
    coef = np.polyfit(t, v, 1)
    resid = v - np.polyval(coef, t)
    r1 = float(np.corrcoef(resid[:-1], resid[1:])[0, 1]) if len(resid) > 2 else 0.0
    r1 = min(max(r1, -0.999), 0.999)
    neff = max(3.0, len(v) * (1 - r1) / (1 + r1))
    # AR(1)-lag-1 Neff is a rough calibration: block-bootstrap cross-checks
    # showed it can be ~2x anti-conservative on strongly structured residuals.
    # Treat the CI as indicative; verdicts here were confirmed against a 2 h
    # moving-block bootstrap (2026-07-14 adversarial verification).
    se = float(resid.std(ddof=1)) / (float(t.std()) * math.sqrt(neff))
    return float(coef[0]), 1.96 * se


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", default=DRIFT24_RUNS,
                        help="comma-separated air run ids to stitch")
    parser.add_argument("--log-dir", default=str(LOG_DIR))
    parser.add_argument("--skip-first-min", type=float, default=30.0)
    parser.add_argument("--out-prefix", default=None)
    args = parser.parse_args()

    ok = load_stitched(args.runs, Path(args.log_dir))
    t_s = ok["time_s"].to_numpy(dtype=float)
    keep = t_s >= t_s[0] + args.skip_first_min * 60.0
    ok, t_s = ok[keep].reset_index(drop=True), t_s[keep]

    x = ok["raw_x"].to_numpy(dtype=float)
    y = ok["raw_y"].to_numpy(dtype=float)
    mag = np.hypot(x, y)
    mag_mean = float(mag.mean())
    rel = (mag / mag_mean - 1.0) * 100  # percent deviation
    temp = ok["temp_c"].to_numpy(dtype=float)
    humid = ok["humid_pct"].to_numpy(dtype=float)
    pcb = ok["ad3_pcb_temp_c"].to_numpy(dtype=float)
    dur_h = (t_s[-1] - t_s[0]) / 3600.0
    print(f"rows={len(ok)}  duration={dur_h:.2f} h  temp {temp.min():.2f}..{temp.max():.2f} C  "
          f"humid {humid.min():.1f}..{humid.max():.1f} %  pcb {pcb.min():.2f}..{pcb.max():.2f} C")

    # noise floor: step noise of rel (drift-free)
    noise_pct = float(np.diff(rel).std() / math.sqrt(2))
    print(f"per-row noise floor: {noise_pct:.4f} % (step-based)")

    models: dict[str, np.ndarray] = {}

    # deployed sweep model, applied blind to raw x/y then magnitude
    try:
        sweep = json.loads(TEMP_MODEL_PATH.read_text())["results"]
        lag_s_dep = -4.75 * 60.0
        t_dep = shift_series(temp, t_s, lag_s_dep)
        xc = x - (sweep["raw_x"]["lag_quadratic"]["temp_coef"] * (t_dep - 27.0)
                  + sweep["raw_x"]["lag_quadratic"]["temp2_coef"] * (t_dep**2 - 27.0**2))
        yc = y - (sweep["raw_y"]["lag_quadratic"]["temp_coef"] * (t_dep - 27.0)
                  + sweep["raw_y"]["lag_quadratic"]["temp2_coef"] * (t_dep**2 - 27.0**2))
        mag_dep = np.hypot(xc, yc)
        models["deployed_sweep_model"] = (mag_dep / mag_dep.mean() - 1.0) * 100 \
            - float(((mag_dep / mag_dep.mean() - 1.0) * 100).mean())
    except Exception as exc:
        print(f"[warn] deployed model unavailable: {exc}")

    # self-fitted ladder
    _, r_lin = fit_r2(rel, np.column_stack([temp, humid]))
    models["self_static_linear_TH"] = r_lin
    _, r_quad = fit_r2(rel, np.column_stack([temp, temp**2, humid]))
    models["self_static_quad_TH"] = r_quad

    lags = np.arange(-15 * 60.0, 15 * 60.0 + 1, 30.0)
    best = (None, -np.inf)
    for lag in lags:
        tl = shift_series(temp, t_s, lag)
        _, r = fit_r2(rel, np.column_stack([tl, tl**2, shift_series(humid, t_s, lag)]))
        r2 = 1 - r.var() / rel.var()
        if r2 > best[1]:
            best = (lag, r2)
    best_lag = best[0]
    tl = shift_series(temp, t_s, best_lag)
    hl = shift_series(humid, t_s, best_lag)
    _, r_lag = fit_r2(rel, np.column_stack([tl, tl**2, hl]))
    models[f"self_lag_quad_TH({best_lag/60:+.1f}min)"] = r_lag

    taus = np.array([30, 60, 120, 180, 300, 450, 600, 900, 1200, 1800, 2400, 3600], dtype=float)
    best_t = (None, -np.inf)
    for tau in taus:
        tf = first_order_filter(temp, t_s, tau)
        _, r = fit_r2(rel, np.column_stack([tf, tf**2, first_order_filter(humid, t_s, tau)]))
        r2 = 1 - r.var() / rel.var()
        if r2 > best_t[1]:
            best_t = (tau, r2)
    best_tau = best_t[0]
    tf = first_order_filter(temp, t_s, best_tau)
    hf = first_order_filter(humid, t_s, best_tau)
    _, r_rc = fit_r2(rel, np.column_stack([tf, tf**2, hf]))
    models[f"self_rc_quad_TH(tau={best_tau/60:.1f}min)"] = r_rc
    _, r_rcp = fit_r2(rel, np.column_stack([tf, tf**2, hf, pcb]))
    models[f"self_rc_quad_TH+pcb"] = r_rcp

    # split-half honesty check on the best self model form
    half = len(rel) // 2
    X_rc = np.column_stack([tf, tf**2, hf, pcb])
    A = np.column_stack([np.ones(half), X_rc[:half]])
    coef_h, *_ = np.linalg.lstsq(A, rel[:half], rcond=None)
    oos = rel[half:] - np.column_stack([np.ones(len(rel) - half), X_rc[half:]]) @ coef_h
    oos -= oos.mean()

    print(f"\n=== residual ladder (magnitude, % of mean) ===")
    print(f"{'model':38} {'p95-p05':>8} {'30min-block p95-p05':>20} {'trend %/24h':>16}")
    print(f"{'raw (no correction)':38} {span(rel):>8.4f} "
          f"{span(block_means(rel, t_s, 1800)):>20.4f} "
          f"{trend_per_24h(rel, t_s)[0]:>+10.4f}+-{trend_per_24h(rel, t_s)[1]:.4f}")
    results = {}
    for name, resid in models.items():
        bm = block_means(resid, t_s, 1800)
        slope, ci = trend_per_24h(resid, t_s)
        results[name] = {
            "p95_p05_pct": span(resid),
            "block30_p95_p05_pct": span(bm),
            "trend_pct_per_24h": slope,
            "trend_ci95": ci,
        }
        print(f"{name:38} {span(resid):>8.4f} {span(bm):>20.4f} {slope:>+10.4f}+-{ci:.4f}")
    print(f"{'split-half OOS (rc_quad_TH+pcb)':38} {span(oos):>8.4f} "
          f"{span(block_means(oos, t_s[half:], 1800)):>20.4f}")

    # structure of the best self-fitted residual
    resid = r_rcp - r_rcp.mean()
    print(f"\n=== structure of best self-fitted residual ===")
    for block_min in (5, 15, 30, 60, 120):
        bm = block_means(resid, t_s, block_min * 60)
        expected_noise = noise_pct / math.sqrt(block_min * 60 / np.median(np.diff(t_s)))
        print(f"block {block_min:>3} min: std {bm.std(ddof=1):.4f} %  "
              f"(white-noise expectation {expected_noise:.4f} %)")
    slope, ci = trend_per_24h(resid, t_s)
    print(f"residual linear trend: {slope:+.4f} +- {ci:.4f} %/24h "
          f"({'significant' if abs(slope) > ci else 'not significant'})")
    for col in ("temp_c", "humid_pct", "ad3_pcb_temp_c", "ad3_fpga_temp_c", "ad3_usb_v"):
        if col in ok and ok[col].notna().mean() > 0.8:
            v = ok[col].astype(float).to_numpy()
            if np.nanstd(v) > 1e-12:
                m = np.isfinite(v)
                print(f"corr(residual, {col}) = {np.corrcoef(resid[m], v[m])[0, 1]:+.3f}")

    # spectrum: HVAC line attenuation (uniform resample over small gaps)
    grid = np.arange(t_s[0], t_s[-1], 10.0)
    rel_g = np.interp(grid, t_s, rel)
    res_g = np.interp(grid, t_s, resid)
    freqs = np.fft.rfftfreq(len(grid), 10.0)
    def amp_at(v, period_s):
        a = np.abs(np.fft.rfft(v - v.mean())) * 2 / len(v)
        band = (freqs > 1 / (period_s * 1.4)) & (freqs < 1 / (period_s * 0.7))
        return float(a[band].max()) if band.any() else float("nan")
    hvac_raw = amp_at(rel_g, 45 * 60)
    hvac_res = amp_at(res_g, 45 * 60)
    print(f"~45 min HVAC line amplitude: raw {hvac_raw:.4f} % -> residual {hvac_res:.4f} % "
          f"({hvac_raw / hvac_res:.1f}x attenuation)" if hvac_res > 0 else "")

    # figure
    fig, axes = plt.subplots(3, 1, figsize=(13, 8), dpi=140, sharex=True)
    fig.patch.set_facecolor(SURFACE)
    t_h = (t_s - t_s[0]) / 3600.0
    for ax in axes:
        ax.set_facecolor(SURFACE)
        ax.grid(True, color=GRID, linewidth=0.8)
        ax.tick_params(colors=INK_2, labelsize=8)
        for sp in ax.spines.values():
            sp.set_color(GRID)
    axes[0].plot(t_h, rel - rel.mean(), color=C_NEUTRAL, linewidth=0.8)
    axes[0].set_ylabel("raw (%)", color=INK_2)
    axes[0].set_title("air-run |ratio| deviation: raw vs corrected residuals", color=INK)
    if "deployed_sweep_model" in models:
        axes[1].plot(t_h, models["deployed_sweep_model"], color=C_3, linewidth=0.8)
    axes[1].set_ylabel("deployed corr. (%)", color=INK_2)
    axes[2].plot(t_h, resid, color=C_1, linewidth=0.8)
    bm_t = block_means(t_h, t_s, 1800)
    bm_v = block_means(resid, t_s, 1800)
    axes[2].plot(bm_t, bm_v, color=C_2, linewidth=2.0)
    axes[2].set_ylabel("best self-fit (%)", color=INK_2)
    axes[2].set_xlabel("elapsed (h)", color=INK_2)
    prefix = args.out_prefix or str(ANALYSIS_DIR / "residual_drift_drift24")
    fig.tight_layout()
    fig.savefig(f"{prefix}.png", facecolor=SURFACE)
    plt.close(fig)

    summary = {
        "runs": args.runs,
        "duration_h": dur_h,
        "noise_floor_pct": noise_pct,
        "raw": {"p95_p05_pct": span(rel),
                "block30_p95_p05_pct": span(block_means(rel, t_s, 1800))},
        "models": results,
        "split_half_oos_p95_p05_pct": span(oos),
        "best_lag_min": best_lag / 60.0,
        "best_tau_min": best_tau / 60.0,
        "hvac_line_raw_pct": hvac_raw,
        "hvac_line_residual_pct": hvac_res,
    }
    Path(f"{prefix}.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nwrote {prefix}.png / .json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
